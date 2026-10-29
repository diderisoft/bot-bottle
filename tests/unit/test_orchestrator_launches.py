"""Unit: remote launch and attach relayed from the orchestrator to the host
controller (PRD 0092 chunk 6): `POST /launches`, `GET /launches/{job}[/events]`
and the `GET /bottles/{id}/attach` WebSocket, against a mocked and a real
in-process host controller, and 503 without one."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY, mint
from bot_bottle.contract.hostctl import (
    CONFIRM_ERROR, HOST_CONTROL, HOSTCTL_URL_ENV, ROLE_ORCHESTRATOR,
)
from bot_bottle.contract.http import ORCHESTRATOR_AUTH_HEADER
from bot_bottle.control.hostctl import sessions
from bot_bottle.control.hostctl.bottles import BottleService
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.lib import websocket as ws
from bot_bottle.orchestrator.api import create_app
from bot_bottle.orchestrator.hostctl_relay import HostControlRelay
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.registry_store import RegistryStore
from bot_bottle.orchestrator.store.store_manager import StoreManager

from . import use_bottle_root
from .test_hostctl_bottles import FakeBackend

_CP_KEY = "control-plane-key"
_HC_KEY = "h" * 32


def _orchestrator(root: Path) -> OrchestratorCore:
    store = RegistryStore(root / "r.db")
    store.migrate()
    StoreManager(store.db_path).migrate()
    return OrchestratorCore(store)


class _App(unittest.TestCase):
    hostctl: HostControlRelay | None = None

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.orch = _orchestrator(root)
        self.client = self.enterContext(TestClient(
            create_app(self.orch, signing_key=_CP_KEY, hostctl=self.hostctl)))

    def headers(self, role: str = ROLE_CLI) -> dict[str, str]:
        return {ORCHESTRATOR_AUTH_HEADER: mint(role, _CP_KEY)}


class TestWithoutHostController(_App):
    def test_launch_routes_answer_503(self) -> None:
        resp = self.client.post("/launches", json={"agent": "demo"}, headers=self.headers())
        self.assertEqual((503, "host controller not available"),
                         (resp.status_code, resp.json()["detail"]))
        self.assertEqual(503, self.client.get("/launches/abc", headers=self.headers()).status_code)

    def test_attach_closes(self) -> None:
        with self.assertRaises(WebSocketDisconnect) as cm, \
                self.client.websocket_connect("/bottles/b1/attach", headers=self.headers()):
            pass
        self.assertEqual(1011, cm.exception.code)

    def test_relay_comes_from_the_provisioned_env(self) -> None:
        self.assertIsNone(HostControlRelay.from_env({}))
        self.assertIsNone(HostControlRelay.from_env({HOSTCTL_URL_ENV: "http://h:8098"}))
        relay = HostControlRelay.from_env({HOSTCTL_URL_ENV: "http://h:8098/",
                                           HOST_CONTROL.token_env: "t"})
        self.assertEqual(HostControlRelay("http://h:8098", "t"), relay)
        self.assertNotIn("'t'", repr(relay))


class TestRelayMocked(_App):
    hostctl = HostControlRelay("http://hostctl:8098", "orch-token")

    def test_launch_forwards_only_the_remote_fields(self) -> None:
        with mock.patch.object(HostControlRelay, "request",
                               return_value=(202, {"job": "j1"})) as request:
            resp = self.client.post("/launches", headers=self.headers(), json={
                "agent": "demo", "prompt": "go", "headless": True, "assume_yes": True,
                "cwd": "/etc", "secrets": {"T": "v"}})
        self.assertEqual((202, {"job": "j1"}), (resp.status_code, resp.json()))
        method, path, body = request.call_args.args
        self.assertEqual(("POST", "/bottles"), (method, path))
        self.assertEqual("demo", body["agent"])
        self.assertNotIn("cwd", body)
        self.assertNotIn("secrets", body)

    def test_launch_is_validated_before_relaying(self) -> None:
        with mock.patch.object(HostControlRelay, "request") as request:
            resp = self.client.post("/launches", headers=self.headers(),
                                    json={"agent": "../etc"})
            self.assertEqual(400, resp.status_code)
            resp = self.client.post("/launches", headers=self.headers(),
                                    json={"agent": "demo", "headless": True})
            self.assertEqual(400, resp.status_code)  # headless needs a prompt
        request.assert_not_called()

    def test_only_the_cli_role(self) -> None:
        resp = self.client.post("/launches", json={"agent": "demo"},
                                headers=self.headers(ROLE_GATEWAY))
        self.assertEqual(403, resp.status_code)
        self.assertEqual(401, self.client.post("/launches", json={"agent": "demo"}).status_code)
        for headers in ({}, self.headers(ROLE_GATEWAY)):
            with self.subTest(headers=headers), self.assertRaises(WebSocketDisconnect) as cm, \
                    self.client.websocket_connect("/bottles/b1/attach", headers=headers):
                pass
            self.assertEqual(1008, cm.exception.code)

    def test_job_status_passes_through(self) -> None:
        with mock.patch.object(HostControlRelay, "request",
                               return_value=(404, {"error": "no job j9"})) as request:
            resp = self.client.get("/launches/j9", headers=self.headers())
        self.assertEqual((404, {"error": "no job j9"}), (resp.status_code, resp.json()))
        self.assertEqual(("GET", "/jobs/j9"), request.call_args.args[:2])
        self.assertEqual(400, self.client.get("/launches/-bad", headers=self.headers())
                         .status_code)

    def test_unreachable_is_502(self) -> None:
        resp = self.client.post("/launches", headers=self.headers(), json={"agent": "demo"})
        self.assertEqual(502, resp.status_code)


class _FakeAttach:
    """A host controller attach endpoint: completes the handshake, records
    the request, echoes frames back, then sends `exit` and closes on `bye`."""

    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.request_line = ""
        self.headers: dict[str, str] = {}
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        self.listener.close()
        reader = conn.makefile("rb")
        writer = conn.makefile("wb", buffering=0)
        self.request_line = reader.readline().decode().strip()
        while line := reader.readline().decode().strip():
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        writer.write((
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {ws.accept_key(self.headers['sec-websocket-key'])}\r\n\r\n"
        ).encode())
        while True:
            opcode, payload = ws.read_frame(cast(Any, reader))
            if opcode == ws.TEXT and payload == b"bye":
                ws.write_frame(cast(Any, writer), ws.TEXT, b'{"type": "exit", "status": 0}')
                ws.write_frame(cast(Any, writer), ws.CLOSE, b"")
                break
            ws.write_frame(cast(Any, writer), opcode, payload)
        conn.close()


class TestAttachRelay(_App):
    def setUp(self) -> None:
        self.fake = _FakeAttach()
        self.hostctl = HostControlRelay(f"http://127.0.0.1:{self.fake.port}", "orch-token")
        super().setUp()

    def test_frames_pass_through_both_ways(self) -> None:
        rec = self.orch.register_bottle(
            "10.243.0.2", metadata=json.dumps({"slug": "demo-abc12"}))
        with self.client.websocket_connect(
                f"/bottles/{rec.bottle_id}/attach?cols=100&rows=30",
                headers=self.headers()) as viewer:
            viewer.send_bytes(b"ls\r")
            self.assertEqual(b"ls\r", viewer.receive_bytes())
            viewer.send_text('{"type": "resize", "cols": 90, "rows": 20}')
            self.assertEqual("resize", json.loads(viewer.receive_text())["type"])
            viewer.send_text("bye")
            self.assertEqual("exit", json.loads(viewer.receive_text())["type"])
        # The registry id named its session by slug, as the orchestrator role.
        self.assertEqual("GET /sessions/demo-abc12/attach?cols=100&rows=30 HTTP/1.1",
                         self.fake.request_line)
        self.assertEqual("Bearer orch-token", self.fake.headers["authorization"])


@unittest.skipUnless(importlib.util.find_spec("wsproto"), "uvicorn needs wsproto for WebSockets")
class TestAttachOverUvicorn(unittest.TestCase):
    """The relay through the real server, as the orchestrator image runs it."""

    def test_frames_pass_through(self) -> None:
        from bot_bottle.orchestrator.server import make_server  # pylint: disable=import-outside-toplevel
        fake = _FakeAttach()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server = make_server(_orchestrator(root), signing_key=_CP_KEY, hostctl=HostControlRelay(
            f"http://127.0.0.1:{fake.port}", "orch-token"))
        threading.Thread(target=server.run, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        for _ in range(100):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 1):
                break
        sock, reader, writer = ws.connect(
            "127.0.0.1", port, "/bottles/demo-1/attach",
            headers={ORCHESTRATOR_AUTH_HEADER: mint(ROLE_CLI, _CP_KEY)})
        self.addCleanup(sock.close)
        ws.write_frame(writer, ws.BINARY, b"hi", mask=True)
        self.assertEqual((ws.BINARY, b"hi"), ws.read_frame(reader))
        ws.write_frame(writer, ws.TEXT, b"bye", mask=True)
        self.assertEqual(ws.TEXT, ws.read_frame(reader)[0])
        self.assertTrue(fake.request_line.startswith("GET /sessions/demo-1/attach?"))


class TestRealHostController(_App):
    """The relay against a real in-process host controller and a fake backend."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbo.")))
        self.addCleanup(use_bottle_root(root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        manager = sessions.SessionManager({})
        self.backend = FakeBackend()
        service = BottleService(manager, backend_for=lambda _s: cast(Any, self.backend),
                                manifest_for=lambda _cwd: mock.MagicMock(),
                                secrets_file=root / "secrets")
        server = HostControlServer(("127.0.0.1", 0), "b", _HC_KEY, manager, service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        self.hostctl = HostControlRelay(f"http://127.0.0.1:{server.server_address[1]}",
                                        HOST_CONTROL.mint(ROLE_ORCHESTRATOR, _HC_KEY))
        super().setUp()

    def test_launch_job_and_events_round_trip(self) -> None:
        self.backend.stale = True  # stops the job at the one yes/no left
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml", return_value=""):
            resp = self.client.post("/launches", headers=self.headers(),
                                    json={"agent": "demo", "label": "r1"})
            self.assertEqual(202, resp.status_code)
            job_id = resp.json()["job"]
            # The job belongs to the orchestrator role.
            self.assertIsNotNone(self.server.jobs.get(job_id, ROLE_ORCHESTRATOR))
            with self.client.stream("GET", f"/launches/{job_id}/events",
                                    headers=self.headers()) as events:
                lines = [json.loads(line) for line in events.iter_lines() if line]
        # The job stops for the yes/no the remote caller resends.
        self.assertEqual(("error", CONFIRM_ERROR), (lines[-1]["type"], lines[-1].get("code")))
        status = self.client.get(f"/launches/{job_id}", headers=self.headers()).json()
        self.assertEqual(job_id, status["id"])
        self.assertEqual(404, self.client.get("/launches/nope", headers=self.headers())
                         .status_code)
        self.assertEqual(404, self.client.get("/launches/nope/events", headers=self.headers())
                         .status_code)


if __name__ == "__main__":
    unittest.main()
