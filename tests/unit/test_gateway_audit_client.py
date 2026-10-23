"""Unit: the gateway's audit client, against a real local HTTP server."""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from bot_bottle.contract.audit import AUDIT_EVENTS_PATH, AuditEvent
from bot_bottle.gateway.audit_client import GatewayAudit
from bot_bottle.gateway.policy_resolver import ORCHESTRATOR_AUTH_HEADER, ORCHESTRATOR_AUTH_JWT_ENV


class _Orchestrator(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.status = 200
        self.received: list[tuple[str, str, dict[str, Any]]] = []
        self.got = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server: Any = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.received.append((self.path, self.headers.get(ORCHESTRATOR_AUTH_HEADER, ""), body))
        server.got.set()
        self.send_response(server.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        del format, args


def _event() -> AuditEvent:
    return AuditEvent(type="egress.decision", actor="bottled-agent:a", action="egress.get",
                      outcome="allowed", sensitivity="security", ts_event="t")


class GatewayAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _Orchestrator()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self) -> GatewayAudit:
        audit = GatewayAudit(self.url, "egress", retry_seconds=0.01).start()
        self.addCleanup(audit.close, 1.0)
        return audit

    def test_posts_the_event_with_its_producer_and_the_gateway_token(self) -> None:
        with mock.patch.dict("os.environ", {ORCHESTRATOR_AUTH_JWT_ENV: "gw.jwt"}):
            self.client().submit(_event())
            self.assertTrue(self.server.got.wait(5))
        path, auth, body = self.server.received[0]
        self.assertEqual((AUDIT_EVENTS_PATH, "gw.jwt", "egress"), (path, auth, body["producer"]))
        self.assertEqual("egress.decision", body["event"]["type"])

    def test_a_refused_event_is_dropped(self) -> None:
        self.server.status = 400
        audit = self.client()
        with redirect_stderr(io.StringIO()) as err:
            audit.submit(_event())
            self.assertTrue(self.server.got.wait(5))
            idle = threading.Event()
            for _ in range(500):
                if not audit.pending():
                    break
                idle.wait(0.01)
        self.assertEqual(0, audit.pending())
        self.assertIn("dropping", err.getvalue())


if __name__ == "__main__":
    unittest.main()
