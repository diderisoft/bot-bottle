"""Unit tests for the orchestrator HTTP control plane (PRD 0070).

Mostly exercises the pure `dispatch()` (socket-free, like the supervise
server tests), plus one real-socket round-trip to prove the handler wiring.
"""

from __future__ import annotations

import base64
import io
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
from unittest.mock import MagicMock, patch

from bot_bottle.orchestrator_auth import ROLE_CLI, ROLE_GATEWAY, mint
from bot_bottle.orchestrator.broker import StubBroker
from bot_bottle.orchestrator.server import dispatch, make_server
from bot_bottle.orchestrator.store.registry_store import BottleRecord, RegistryStore
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.store_manager import StoreManager
from bot_bottle.orchestrator.supervisor import (
    Proposal,
    TOOL_EGRESS_ALLOW,
    Supervisor,
)


def _body(obj: object) -> bytes:
    return json.dumps(obj).encode()


def _orchestrator(db_path: Path) -> OrchestratorCore:
    store = RegistryStore(db_path)
    store.migrate()
    secret = secrets.token_bytes(16)
    return OrchestratorCore(store, StubBroker(secret), secret)


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

    def test_register_and_reprovision_encrypted_tokens(self) -> None:
        key = base64.urlsafe_b64encode(b"unit-test-key").rstrip(b"=").decode()
        status, payload = dispatch(
            self.orch, "POST", "/bottles", _body({
                "source_ip": "10.243.0.11",
                "tokens": {"EGRESS_TOKEN_0": "upstream-secret"},
                "env_var_secret": key,
            }),
        )
        self.assertEqual(201, status)
        bottle_id = payload["bottle_id"]
        assert isinstance(bottle_id, str)
        self.orch._tokens.clear()
        status, response = dispatch(
            self.orch, "POST", f"/bottles/{bottle_id}/reprovision_gateway",
            _body({"env_var_secret": key}),
        )
        self.assertEqual((200, {"reprovisioned": True}), (status, response))
        self.assertEqual(
            {"EGRESS_TOKEN_0": "upstream-secret"}, self.orch.tokens_for(bottle_id),
        )

    def test_reprovision_validates_request_and_missing_rows(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway", b"not-json",
        )
        self.assertEqual(400, status)
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway", _body({}),
        )
        self.assertEqual(400, status)
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway",
            _body({"env_var_secret": "key"}),
        )
        self.assertEqual(404, status)

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

    def test_internal_failure_is_contextual_but_redacted(self) -> None:
        orch = MagicMock()
        orch.registry.all.side_effect = RuntimeError("SENSITIVE request value")
        with patch("sys.stderr", io.StringIO()) as stderr:
            server = make_server(orch, "127.0.0.1", 0)
            self.addCleanup(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            host, port = server.server_address[0], server.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f"http://{host}:{port}/bottles", timeout=5)
            payload = json.loads(raised.exception.read())
            output = stderr.getvalue()
        self.assertEqual({"error": "internal error"}, payload)
        self.assertIn("GET /bottles", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("SENSITIVE", output)


class TestOrchestratorAuth(unittest.TestCase):
    """Role-scoped control-plane tokens (issue #400 / #469 review): every route
    but /health needs a valid token, and the token's role gates which routes it
    reaches — a `gateway` data-plane token can't drive the operator routes."""

    _OPERATOR_ROUTES = [
        ("GET", "/bottles", b""),
        ("POST", "/bottles", _body({"source_ip": "10.0.0.1"})),
        ("PUT", "/bottles/x/policy", _body({"policy": "routes: []"})),
        ("DELETE", "/bottles/x", b""),
        ("POST", "/reconcile", _body({"live_source_ips": []})),
        ("POST", "/attribute", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
        ("GET", "/supervise/proposals", b""),
        ("POST", "/supervise/respond",
         _body({"proposal_id": "p", "bottle_slug": "s", "decision": "a"})),
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def test_health_is_public_even_unauthenticated(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/health", b"", role=None)
        self.assertEqual(200, status)

    def test_unauthenticated_denies_every_other_route(self) -> None:
        routes = self._OPERATOR_ROUTES + [
            ("POST", "/resolve", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
            ("POST", "/supervise/propose",
             _body({"source_ip": "1", "tool": "egress-allow", "proposed_file": "x", "justification": "j"})),
            ("POST", "/supervise/poll", _body({"source_ip": "1", "proposal_id": "p"})),
        ]
        for method, path, body in routes:
            status, _ = dispatch(self.orch, method, path, body, role=None)
            self.assertEqual(401, status, f"{method} {path} should be 401 unauthenticated")

    def test_gateway_role_denied_on_operator_routes(self) -> None:
        # A compromised data-plane process holds only a `gateway` token — it must
        # not reach the operator routes (approve proposals, rewrite policy, …).
        for method, path, body in self._OPERATOR_ROUTES:
            status, payload = dispatch(self.orch, method, path, body, role=ROLE_GATEWAY)
            self.assertEqual(403, status, f"{method} {path} should be 403 for gateway")
            self.assertIn("insufficient role", str(payload.get("error")))

    def test_gateway_role_allowed_on_data_routes(self) -> None:
        # The gateway CAN reach its own lookups. Register a bottle so /resolve
        # returns 200 rather than a 403 — proving the role gate let it through.
        rec = self.orch.registry.register("10.0.0.9", policy="routes: []", metadata="")
        status, _ = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.0.0.9", "identity_token": rec.identity_token}),
            role=ROLE_GATEWAY)
        self.assertEqual(200, status)

    def test_deny_happens_before_the_registry_is_touched(self) -> None:
        """An unauthenticated DELETE must not tear a bottle down. 401, and the
        bottle is still there."""
        rec = self.orch.registry.register("10.0.0.9", policy="", metadata="")
        status, _ = dispatch(
            self.orch, "DELETE", f"/bottles/{rec.bottle_id}", b"", role=None)
        self.assertEqual(401, status)
        self.assertIsNotNone(self.orch.registry.get(rec.bottle_id))

    def _server_with_key(self, signing_key: str):
        with patch.dict("os.environ", {"BOT_BOTTLE_ORCHESTRATOR_TOKEN": signing_key}):
            server = make_server(self.orch, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        return f"http://{host}:{port}"

    def _status(self, url: str, *, header: str | None = None) -> int:
        req = urllib.request.Request(url)
        if header is not None:
            req.add_header("x-bot-bottle-orchestrator-auth", header)
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_configured_server_enforces_roles_over_http(self) -> None:
        key = "test-key"
        base = self._server_with_key(key)
        gateway_tok = mint(ROLE_GATEWAY, key)
        cli_tok = mint(ROLE_CLI, key)
        # /health is public — no header needed.
        self.assertEqual(200, self._status(f"{base}/health"))
        # /bottles (operator) needs a valid cli token.
        self.assertEqual(401, self._status(f"{base}/bottles"))
        self.assertEqual(401, self._status(f"{base}/bottles", header="wrong"))
        self.assertEqual(403, self._status(f"{base}/bottles", header=gateway_tok))
        self.assertEqual(200, self._status(f"{base}/bottles", header=cli_tok))

    def test_unconfigured_server_runs_open(self) -> None:
        """No signing key set (tests / nft-protected Firecracker): open mode
        grants full cli access, so existing round-trip behavior is unchanged."""
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("BOT_BOTTLE_ORCHESTRATOR_TOKEN", None)
            server = make_server(self.orch, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.assertEqual(ROLE_CLI, server.role_for(""))
        self.assertEqual(ROLE_CLI, server.role_for("anything"))


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
        self.orch = OrchestratorCore(self.store, StubBroker(secret), secret)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _queue(self, slug: str, proposed: str) -> str:
        self.store.register(
            "10.243.0.1", metadata=json.dumps({"slug": slug}), policy="routes: []\n")
        p = Proposal.new(
            bottle_slug=slug, tool=TOOL_EGRESS_ALLOW, proposed_file=proposed,
            justification="need it")
        Supervisor().write_proposal(p)
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


class TestDispatchSuperviseAgentRpc(unittest.TestCase):
    """The agent half — `/supervise/propose` + `/supervise/poll` — attributed by
    (source_ip, identity_token) like /resolve, so a bottle can only ever queue
    or read its own proposals (PRD 0070 / issue #469)."""

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
        self.orch = OrchestratorCore(self.store, StubBroker(secret), secret)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _register(self, source_ip: str = "10.243.0.9", slug: str = "demo"):
        return self.store.register(
            source_ip, metadata=json.dumps({"slug": slug}), policy="routes: []\n")

    def _propose(self, rec: BottleRecord, proposed: str = "routes:\n  - host: g.com\n"):
        return dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": proposed, "justification": "need it",
        }))

    def _poll(self, rec: BottleRecord, proposal_id: str):
        return dispatch(self.orch, "POST", "/supervise/poll", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "proposal_id": proposal_id,
        }))

    def test_propose_queues_under_the_resolved_bottle(self) -> None:
        rec = self._register()
        status, payload = self._propose(rec)
        self.assertEqual(201, status)
        pid = payload["proposal_id"]
        assert isinstance(pid, str) and pid
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        proposals = listing["proposals"]
        assert isinstance(proposals, list)
        self.assertEqual(pid, proposals[0]["id"])
        # Queued under the orchestrator-resolved bottle id, never a caller slug.
        self.assertEqual(rec.bottle_id, proposals[0]["bottle_slug"])

    def test_propose_unattributed_is_403(self) -> None:
        status, payload = dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": "10.9.9.9", "identity_token": "wrong",
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": "x\n", "justification": "j",
        }))
        self.assertEqual(403, status)
        self.assertIn("unattributed", str(payload["error"]))

    def test_propose_rejects_unknown_tool(self) -> None:
        rec = self._register()
        status, _ = dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": "not-a-tool", "proposed_file": "x\n", "justification": "j",
        }))
        self.assertEqual(400, status)

    def test_poll_pending_then_decided_then_idempotent(self) -> None:
        rec = self._register()
        _, proposed = self._propose(rec)
        pid = proposed["proposal_id"]
        assert isinstance(pid, str)

        status, poll = self._poll(rec, pid)
        self.assertEqual(200, status)
        self.assertEqual("pending", poll["status"])

        # Operator decides server-side.
        dispatch(self.orch, "POST", "/supervise/respond", _body({
            "proposal_id": pid, "bottle_slug": rec.bottle_id,
            "decision": "approve", "notes": "ok",
        }))
        _, decided = self._poll(rec, pid)
        self.assertEqual("approved", decided["status"])
        self.assertEqual("ok", decided["notes"])

        # Poll doesn't archive: it's gone from the operator's pending list, but a
        # re-poll returns the same decision so a dropped connection can't lose it.
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual([], listing["proposals"])
        _, again = self._poll(rec, pid)
        self.assertEqual(decided, again)

    def test_poll_cannot_read_another_bottles_proposal(self) -> None:
        rec_a = self._register("10.0.0.1", "a")
        rec_b = self._register("10.0.0.2", "b")
        _, proposed = self._propose(rec_a)
        pid = proposed["proposal_id"]
        assert isinstance(pid, str)
        # b polls a's proposal id: scoped to b's own queue → never a's response.
        status, poll = self._poll(rec_b, pid)
        self.assertEqual(200, status)
        self.assertEqual("unknown", poll["status"])

    # --- request validation (400) + fail-closed (403) ----------------------

    def test_propose_invalid_json_is_400(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/supervise/propose", b"{not json")
        self.assertEqual(400, status)

    def test_propose_missing_fields_are_400(self) -> None:
        rec = self._register()
        base = {
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": "routes:\n", "justification": "j",
        }
        for drop in ("source_ip", "proposed_file", "justification"):
            body = {k: v for k, v in base.items() if k != drop}
            status, _ = dispatch(self.orch, "POST", "/supervise/propose", _body(body))
            self.assertEqual(400, status, drop)

    def test_poll_invalid_json_is_400(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/supervise/poll", b"{not json")
        self.assertEqual(400, status)

    def test_poll_missing_fields_are_400(self) -> None:
        rec = self._register()
        for body in ({"identity_token": rec.identity_token, "proposal_id": "p"},
                     {"source_ip": rec.source_ip, "identity_token": rec.identity_token}):
            status, _ = dispatch(self.orch, "POST", "/supervise/poll", _body(body))
            self.assertEqual(400, status)

    def test_poll_unattributed_is_403(self) -> None:
        status, payload = dispatch(self.orch, "POST", "/supervise/poll", _body({
            "source_ip": "10.9.9.9", "identity_token": "wrong", "proposal_id": "p",
        }))
        self.assertEqual(403, status)
        self.assertIn("unattributed", str(payload["error"]))


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

    def test_non_string_entries_are_rejected(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/reconcile",
            _body({"live_source_ips": [None, 7, "10.0.0.9"]}))
        self.assertEqual(400, status)
        self.assertIn("live_source_ips", str(payload["error"]))

    def test_empty_live_source_ip_is_rejected(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/reconcile", _body({"live_source_ips": [""]}))
        self.assertEqual(400, status)
        self.assertIn("live_source_ips", str(payload["error"]))

    def test_invalid_grace_seconds_is_rejected(self) -> None:
        for value in (True, "30", -1, float("inf"), float("nan")):
            with self.subTest(value=value):
                status, payload = dispatch(
                    self.orch, "POST", "/reconcile",
                    _body({"live_source_ips": [], "grace_seconds": value}))
                self.assertEqual(400, status)
                self.assertIn("grace_seconds", str(payload["error"]))
