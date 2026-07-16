"""Unit tests for the orchestrator HTTP control plane (PRD 0070).

Mostly exercises the pure `dispatch()` (socket-free, like the supervise
server tests), plus one real-socket round-trip to prove the handler wiring.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from bot_bottle.orchestrator.broker import StubBroker
from bot_bottle.orchestrator.control_plane import dispatch, make_server
from bot_bottle.orchestrator.registry import RegistryStore
from bot_bottle.orchestrator.service import Orchestrator


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


if __name__ == "__main__":
    unittest.main()
