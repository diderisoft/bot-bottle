"""Unit tests for the orchestrator HTTP control plane (PRD 0070).

Mostly exercises the pure `dispatch()` (socket-free, like the supervise
server tests), plus one real-socket round-trip to prove the handler wiring.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot_bottle.orchestrator.broker import StubBroker
from bot_bottle.orchestrator.control_plane import dispatch, make_server
from bot_bottle.orchestrator.registry import RegistryStore
from bot_bottle.orchestrator.service import Orchestrator
from bot_bottle.store_manager import StoreManager
from bot_bottle.supervise import (
    Proposal,
    TOOL_EGRESS_ALLOW,
    sha256_hex,
    write_proposal,
)


def _body(obj: object) -> bytes:
    return json.dumps(obj).encode()


def _orchestrator(db_path: Path) -> Orchestrator:
    store = RegistryStore(db_path)
    store.migrate()
    secret = secrets.token_bytes(16)
    return Orchestrator(store, StubBroker(secret), secret)


class TestDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_health(self) -> None:
        status, payload = dispatch(self.orch, "GET", "/health", b"")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_register_returns_id_and_token(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"})
        )
        self.assertEqual(201, status)
        self.assertTrue(payload["bottle_id"])
        self.assertTrue(payload["identity_token"])

    def test_register_requires_source_ip(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/bottles", _body({}))
        self.assertEqual(400, status)

    def test_register_rejects_bad_json(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/bottles", b"{not json")
        self.assertEqual(400, status)

    def test_list_redacts_identity_token(self) -> None:
        dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        status, payload = dispatch(self.orch, "GET", "/bottles", b"")
        self.assertEqual(200, status)
        bottles = payload["bottles"]
        assert isinstance(bottles, list)
        self.assertEqual(1, len(bottles))
        first = bottles[0]
        assert isinstance(first, dict)
        self.assertNotIn("identity_token", first)
        self.assertEqual("10.243.0.1", first["source_ip"])

    def test_attribute_ok_and_forbidden(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.5"})
        )
        token = reg["identity_token"]
        ok_status, ok = dispatch(
            self.orch, "POST", "/attribute",
            _body({"source_ip": "10.243.0.5", "identity_token": token}),
        )
        self.assertEqual(200, ok_status)
        self.assertEqual(reg["bottle_id"], ok["bottle_id"])

        bad_status, _ = dispatch(
            self.orch, "POST", "/attribute",
            _body({"source_ip": "10.243.0.5", "identity_token": "nope"}),
        )
        self.assertEqual(403, bad_status)

    def test_attribute_requires_both_fields(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/attribute", _body({"source_ip": "10.243.0.5"})
        )
        self.assertEqual(400, status)

    def test_delete(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"})
        )
        bid = reg["bottle_id"]
        status, payload = dispatch(self.orch, "DELETE", f"/bottles/{bid}", b"")
        self.assertEqual(200, status)
        self.assertEqual(True, payload["torn_down"])
        self.assertEqual([], self.orch.registry.all())

    def test_delete_missing_404(self) -> None:
        status, _ = dispatch(self.orch, "DELETE", "/bottles/ghost", b"")
        self.assertEqual(404, status)

    def test_unknown_route_404(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/nope", b"")
        self.assertEqual(404, status)

    def test_trailing_slash_normalized(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/health/", b"")
        self.assertEqual(200, status)

    def test_gateway_status_unconfigured(self) -> None:
        status, payload = dispatch(self.orch, "GET", "/gateway", b"")
        self.assertEqual(200, status)
        self.assertEqual(False, payload["configured"])

    def test_launch_stores_policy_and_resolve_returns_it(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.5", "policy": '{"a":1}'}),
        )
        status, payload = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.5", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual(200, status)
        self.assertEqual(reg["bottle_id"], payload["bottle_id"])
        self.assertEqual('{"a":1}', payload["policy"])

    def test_resolve_forbidden_on_bad_token(self) -> None:
        dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.5"}))
        status, _ = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.5", "identity_token": "nope"}),
        )
        self.assertEqual(403, status)

    def test_put_policy_updates_live(self) -> None:
        _, reg = dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        bid = reg["bottle_id"]
        status, payload = dispatch(
            self.orch, "PUT", f"/bottles/{bid}/policy", _body({"policy": '{"v":9}'})
        )
        self.assertEqual(200, status)
        self.assertEqual(True, payload["updated"])
        _, resolved = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.1", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual('{"v":9}', resolved["policy"])

    def test_put_policy_missing_bottle_404(self) -> None:
        status, _ = dispatch(
            self.orch, "PUT", "/bottles/ghost/policy", _body({"policy": "{}"})
        )
        self.assertEqual(404, status)

    def test_put_policy_requires_string(self) -> None:
        _, reg = dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        status, _ = dispatch(
            self.orch, "PUT", f"/bottles/{reg['bottle_id']}/policy", _body({})
        )
        self.assertEqual(400, status)

    def test_resolve_without_token_denies(self) -> None:
        # Mandatory token: source-IP alone no longer resolves (fail-closed 403).
        dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.5", "policy": "P"}),
        )
        status, _ = dispatch(
            self.orch, "POST", "/resolve", _body({"source_ip": "10.243.0.5"})
        )
        self.assertEqual(403, status)

    def test_resolve_with_matching_token(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.6", "policy": "P"}),
        )
        status, payload = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.6", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual(200, status)
        self.assertEqual(reg["bottle_id"], payload["bottle_id"])
        self.assertEqual("P", payload["policy"])

    def test_resolve_requires_source_ip(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/resolve", _body({}))
        self.assertEqual(400, status)


class TestServerRoundTrip(unittest.TestCase):
    def test_http_register_health_attribute(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orch = _orchestrator(Path(tmp.name) / "r.db")
        server = make_server(orch, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        host, port = server.server_address[0], server.server_address[1]
        base = f"http://{host}:{port}"

        reg = json.load(urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/bottles", data=_body({"source_ip": "10.243.0.7"}),
                method="POST", headers={"Content-Type": "application/json"},
            ), timeout=5,
        ))
        self.assertTrue(reg["bottle_id"])

        health = json.load(urllib.request.urlopen(f"{base}/health", timeout=5))
        self.assertEqual("ok", health["status"])

        attr = json.load(urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/attribute",
                data=_body({"source_ip": "10.243.0.7", "identity_token": reg["identity_token"]}),
                method="POST", headers={"Content-Type": "application/json"},
            ), timeout=5,
        ))
        self.assertEqual(reg["bottle_id"], attr["bottle_id"])


class TestControlPlaneAuth(unittest.TestCase):
    """The per-host control-plane secret (issue #400): every route but /health
    is a trusted-caller op an agent must not be able to drive just because it
    can reach the port."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def test_health_is_public_even_unauthorized(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/health", b"", authorized=False)
        self.assertEqual(200, status)

    def test_unauthorized_denies_every_other_route(self) -> None:
        for method, path, body in [
            ("GET", "/bottles", b""),
            ("POST", "/bottles", _body({"source_ip": "10.0.0.1"})),
            ("PUT", "/bottles/x/policy", _body({"policy": "routes: []"})),
            ("DELETE", "/bottles/x", b""),
            ("POST", "/reconcile", _body({"live_source_ips": []})),
            ("POST", "/resolve", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
            ("POST", "/attribute", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
            ("GET", "/supervise/proposals", b""),
            ("POST", "/supervise/respond", _body({"proposal_id": "p", "bottle_slug": "s", "decision": "approve"})),
        ]:
            status, _ = dispatch(self.orch, method, path, body, authorized=False)
            self.assertEqual(401, status, f"{method} {path} should be 401 unauthorized")

    def test_deny_happens_before_the_registry_is_touched(self) -> None:
        """An unauthorized DELETE must not tear a bottle down. 401, and the
        bottle is still there."""
        rec = self.orch.registry.register("10.0.0.9", policy="", metadata="")
        status, _ = dispatch(
            self.orch, "DELETE", f"/bottles/{rec.bottle_id}", b"", authorized=False)
        self.assertEqual(401, status)
        self.assertIsNotNone(self.orch.registry.get(rec.bottle_id))

    def _server_with_secret(self, secret: str):
        with patch.dict("os.environ", {"BOT_BOTTLE_CONTROL_PLANE_TOKEN": secret}):
            server = make_server(self.orch, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        return f"http://{host}:{port}"

    def _status(self, url: str, *, header: str | None = None) -> int:
        req = urllib.request.Request(url)
        if header is not None:
            req.add_header("x-bot-bottle-control-auth", header)
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_configured_server_enforces_the_header_over_http(self) -> None:
        base = self._server_with_secret("s3cret-admin")
        # /health is public — no header needed.
        self.assertEqual(200, self._status(f"{base}/health"))
        # /bottles requires the secret.
        self.assertEqual(401, self._status(f"{base}/bottles"))
        self.assertEqual(401, self._status(f"{base}/bottles", header="wrong"))
        self.assertEqual(200, self._status(f"{base}/bottles", header="s3cret-admin"))

    def test_unconfigured_server_runs_open(self) -> None:
        """No secret set (tests / nft-protected Firecracker): open mode, so the
        existing round-trip and unit behavior are unchanged."""
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("BOT_BOTTLE_CONTROL_PLANE_TOKEN", None)
            server = make_server(self.orch, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.assertTrue(server.is_authorized(""))
        self.assertTrue(server.is_authorized("anything"))


class TestDispatchSupervise(unittest.TestCase):
    """The /supervise/* routes over the pure dispatch()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        db = root / "db" / "bot-bottle.db"
        db.parent.mkdir(parents=True)
        self._env = patch.dict("os.environ", {
            "BOT_BOTTLE_ROOT": str(root),
            "SUPERVISE_DB_PATH": str(db),
        })
        self._env.start()
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        secret = secrets.token_bytes(16)
        self.orch = Orchestrator(self.store, StubBroker(secret), secret)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _queue(self, slug: str, proposed: str) -> str:
        self.store.register(
            "10.243.0.1", metadata=json.dumps({"slug": slug}), policy="routes: []\n")
        p = Proposal.new(
            bottle_slug=slug, tool=TOOL_EGRESS_ALLOW, proposed_file=proposed,
            justification="need it", current_file_hash=sha256_hex(proposed))
        write_proposal(p)
        return p.id

    def test_list_pending(self) -> None:
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        status, payload = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual(200, status)
        proposals = payload["proposals"]
        assert isinstance(proposals, list)
        self.assertEqual(pid, proposals[0]["id"])

    def test_respond_approve_applies_and_clears(self) -> None:
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        status, payload = dispatch(
            self.orch, "POST", "/supervise/respond",
            _body({"proposal_id": pid, "bottle_slug": "demo", "decision": "approve"}),
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["responded"])
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual([], listing["proposals"])

    def test_respond_requires_fields(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/supervise/respond", _body({"decision": "approve"}))
        self.assertEqual(400, status)

    def test_respond_unknown_proposal_conflicts(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/supervise/respond",
            _body({"proposal_id": "ghost", "bottle_slug": "demo", "decision": "approve"}),
        )
        self.assertEqual(409, status)
        self.assertIn("no such proposal", str(payload["error"]))


if __name__ == "__main__":
    unittest.main()


class TestReconcileRoute(unittest.TestCase):
    """`POST /reconcile` — the host tells the orchestrator which bottles are
    actually up, since the orchestrator can't see the backend from inside the
    infra container."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _old(self, source_ip: str) -> str:
        rec = self.orch.registry.register(source_ip)
        with closing(sqlite3.connect(self.orch.registry.db_path)) as conn:
            conn.execute(
                "UPDATE orchestrator_bottles SET created_at = 0.0 WHERE bottle_id = ?",
                (rec.bottle_id,))
            conn.commit()
        return rec.bottle_id

    def test_reaps_absent_and_reports_ids(self) -> None:
        dead = self._old("10.0.0.1")
        alive = self._old("10.0.0.2")
        status, payload = dispatch(
            self.orch, "POST", "/reconcile", _body({"live_source_ips": ["10.0.0.2"]}))
        self.assertEqual(200, status)
        self.assertEqual([dead], payload["reaped"])
        self.assertIsNone(self.orch.registry.get(dead))
        self.assertIsNotNone(self.orch.registry.get(alive))

    def test_missing_live_source_ips_is_400(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/reconcile", _body({}))
        self.assertEqual(400, status)

    def test_grace_seconds_is_honoured(self) -> None:
        """A grace window wide enough to cover the row protects it."""
        self.orch.registry.register("10.0.0.3")
        status, payload = dispatch(
            self.orch, "POST", "/reconcile",
            _body({"live_source_ips": [], "grace_seconds": 3600}))
        self.assertEqual(200, status)
        self.assertEqual([], payload["reaped"])

    def test_non_string_entries_are_ignored(self) -> None:
        dead = self._old("10.0.0.4")
        status, payload = dispatch(
            self.orch, "POST", "/reconcile",
            _body({"live_source_ips": [None, 7, "10.0.0.9"]}))
        self.assertEqual(200, status)
        self.assertEqual([dead], payload["reaped"])
