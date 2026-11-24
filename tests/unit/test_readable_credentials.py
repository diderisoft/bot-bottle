"""Unit: serving an `agent_can_read` credential to the agent. The supervisor's
`GET /bb-credential/<label>`, the orchestrator's store/fetch routes and core,
and the host controller client's push."""

from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract.auth import ROLE_GATEWAY
from bot_bottle.contract.identity import IDENTITY_HEADER
from bot_bottle.gateway.supervisor.server import ORCHESTRATOR_URL_ENV, MCPHandler
from bot_bottle.orchestrator.service import OrchestratorCore
from tests.unit.test_orchestrator_server import _body, _orchestrator, dispatch


# ── supervisor: GET /bb-credential/<label> ────────────────────────────────────

class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


class TestSupervisorCredentialFetch(unittest.TestCase):

    def _get(self, path: str, *, orch_url: str = "http://orch:8080",
             urlopen: Any = None) -> tuple[int, dict[str, Any], list[Any]]:
        h: MCPHandler = MCPHandler.__new__(MCPHandler)
        h.server = types.SimpleNamespace(policy_resolver=None)  # type: ignore[assignment]
        h.client_address = ("10.0.0.7", 4321)
        h.headers = {IDENTITY_HEADER: "ident-tok"}  # type: ignore[assignment]
        h.path = path
        written: list[tuple[int, dict[str, Any]]] = []
        h._write_json = lambda status, body: written.append((status, body))  # type: ignore[method-assign]  # pylint: disable=protected-access
        requests: list[Any] = []

        def default_urlopen(req: Any, timeout: float = 0) -> _Resp:
            requests.append(req)
            return _Resp(b'{"values": {"token": "tok"}}')

        fake = urlopen or default_urlopen
        with mock.patch.dict("os.environ", {ORCHESTRATOR_URL_ENV: orch_url}), \
             mock.patch("urllib.request.urlopen", side_effect=fake):
            h.do_GET()
        self.assertEqual(len(written), 1)
        return written[0][0], written[0][1], requests

    def test_serves_values_and_audits(self) -> None:
        status, body, reqs = self._get("/bb-credential/my-cred")
        self.assertEqual((status, body), (200, {"values": {"token": "tok"}}))
        fetch, audit = reqs
        self.assertEqual(fetch.full_url, "http://orch:8080/credentials/fetch")
        self.assertEqual(json.loads(fetch.data), {
            "source_ip": "10.0.0.7", "identity_token": "ident-tok", "label": "my-cred"})
        self.assertEqual(audit.full_url, "http://orch:8080/audit/events")
        event = json.loads(audit.data)["event"]
        self.assertEqual((event["kind"], event["label"]), ("credential.fetched", "my-cred"))
        self.assertNotIn("tok", audit.data.decode())

    def test_audit_failure_still_serves(self) -> None:
        calls: list[Any] = []

        def urlopen(req: Any, timeout: float = 0) -> _Resp:
            calls.append(req)
            if req.full_url.endswith("/audit/events"):
                raise OSError("audit down")
            return _Resp(b'{"values": {"token": "tok"}}')

        status, _, _ = self._get("/bb-credential/c", urlopen=urlopen)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 2)

    def test_missing_label(self) -> None:
        self.assertEqual(self._get("/bb-credential/")[0], 400)

    def test_no_orchestrator(self) -> None:
        self.assertEqual(self._get("/bb-credential/c", orch_url="")[0], 503)

    def test_not_found(self) -> None:
        def urlopen(req: Any, timeout: float = 0) -> _Resp:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", mock.Mock(), None)
        status, body, _ = self._get("/bb-credential/c", urlopen=urlopen)
        self.assertEqual(status, 404)
        self.assertIn("not readable", body["error"])

    def test_orchestrator_error(self) -> None:
        def urlopen(req: Any, timeout: float = 0) -> _Resp:
            raise urllib.error.HTTPError(req.full_url, 500, "x", mock.Mock(), None)
        status, body, _ = self._get("/bb-credential/c", urlopen=urlopen)
        self.assertEqual((status, body["error"]), (503, "orchestrator error: 500"))

    def test_orchestrator_unreachable(self) -> None:
        def urlopen(req: Any, timeout: float = 0) -> _Resp:
            raise OSError("refused")
        with mock.patch("sys.stderr", io.StringIO()):
            status, _, _ = self._get("/bb-credential/c", urlopen=urlopen)
        self.assertEqual(status, 503)

    def test_write_json(self) -> None:
        h: MCPHandler = MCPHandler.__new__(MCPHandler)
        h.wfile = io.BytesIO()
        h.request_version = "HTTP/1.1"
        h.requestline = "GET / HTTP/1.1"
        h.client_address = ("10.0.0.7", 1)
        h.command = "GET"
        with mock.patch.object(MCPHandler, "log_message"):
            h._write_json(200, {"a": 1})  # pylint: disable=protected-access
        raw = h.wfile.getvalue()
        self.assertRegex(raw, rb"^HTTP/1\.[01] 200 ")
        self.assertTrue(raw.endswith(b'{"a": 1}'))


# ── orchestrator: store, fetch, forget ────────────────────────────────────────

class TestOrchestratorReadableCredentials(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orch: OrchestratorCore = _orchestrator(Path(self._tmp.name) / "r.db")
        _, reg = dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.5"}))
        self.bottle_id = str(reg["bottle_id"])
        self.token = str(reg["identity_token"])

    def _store(self, bottle_id: str, label: str = "my-cred") -> int:
        status, _ = dispatch(self.orch, "PUT",
                             f"/bottles/{bottle_id}/readable-credential/{label}",
                             _body({"values": {"token": "tok"}}))
        return status

    def _fetch(self, label: str = "my-cred", token: str = "") -> tuple[int, dict[str, object]]:
        return dispatch(self.orch, "POST", "/credentials/fetch", _body({
            "source_ip": "10.243.0.5", "identity_token": token or self.token,
            "label": label}), role=ROLE_GATEWAY)

    def test_store_then_fetch_attributed(self) -> None:
        self.assertEqual(self._store(self.bottle_id), 200)
        status, body = self._fetch()
        self.assertEqual((status, body), (200, {"values": {"token": "tok"}}))

    def test_store_for_unknown_bottle(self) -> None:
        self.assertEqual(self._store("nope"), 404)

    def test_fetch_unknown_label_or_unattributed(self) -> None:
        self._store(self.bottle_id)
        self.assertEqual(self._fetch(label="other")[0], 404)
        self.assertEqual(self._fetch(token="wrong")[0], 404)

    def test_deregister_forgets_them(self) -> None:
        self._store(self.bottle_id)
        self.assertTrue(self.orch.deregister_bottle(self.bottle_id))
        self.assertIsNone(self.orch.fetch_readable_credential(self.bottle_id, "my-cred"))

    def test_retire(self) -> None:
        self._store(self.bottle_id)
        self.orch.retire_readable_credentials(self.bottle_id)
        self.assertIsNone(self.orch.fetch_readable_credential(self.bottle_id, "my-cred"))


if __name__ == "__main__":
    unittest.main()
