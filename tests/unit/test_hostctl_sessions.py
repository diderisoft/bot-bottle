"""Unit: tmux agent sessions, the local socket, and the remote attach stream
(PRD 0092 chunk 4). The tmux tests run against a real tmux server on a
per-test bot-bottle root; they skip where tmux 3.1+ isn't installed."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bot_bottle.contract.auth import ROLE_CLI
from bot_bottle.contract.hostctl import HOST_CONTROL, bearer
from bot_bottle.control.hostctl import localsock, sessions
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.lib import websocket as ws
from bot_bottle.manifest.agent import ManifestAgent
from bot_bottle.manifest.util import ManifestError

from . import use_bottle_root

_TMUX_VERSION = sessions.tmux_version() if shutil.which("tmux") else None
_TMUX_OK = _TMUX_VERSION is not None and _TMUX_VERSION >= sessions.MIN_TMUX_VERSION


def _until(predicate, timeout: float = 10.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestWebSocketFraming(unittest.TestCase):
    def test_accept_key_matches_the_rfc_example(self) -> None:
        self.assertEqual("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="))

    def test_round_trips_masked_and_unmasked_frames_of_every_length_encoding(self) -> None:
        for size in (0, 125, 126, 65535, 70000):
            for mask in (False, True):
                buf = io.BytesIO()
                ws.write_frame(buf, ws.BINARY, b"x" * size, mask=mask)
                buf.seek(0)
                self.assertEqual((ws.BINARY, b"x" * size), ws.read_frame(buf))

    def test_joins_fragments_and_rejects_oversized_frames(self) -> None:
        frames = bytes([0x01, 3]) + b"abc" + bytes([0x80, 3]) + b"def"  # TEXT, then CONT+FIN
        self.assertEqual((ws.TEXT, b"abcdef"), ws.read_frame(io.BytesIO(frames)))
        huge = bytes([0x82, 127]) + (ws.MAX_FRAME_BYTES + 1).to_bytes(8, "big")
        with self.assertRaises(ws.WebSocketError):
            ws.read_frame(io.BytesIO(huge))
        with self.assertRaises(EOFError):
            ws.read_frame(io.BytesIO(b"\x82"))


class TestAgentManifestRemote(unittest.TestCase):
    def test_remote_writable(self) -> None:
        self.assertFalse(ManifestAgent.from_dict("a", {}, set()).remote_writable)
        self.assertTrue(
            ManifestAgent.from_dict("a", {"remote": {"writable": True}}, set()).remote_writable)
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"remote": {"writable": "yes"}}, set())
        with self.assertRaises(ManifestError):
            ManifestAgent.from_dict("a", {"remote": {"bogus": True}}, set())


@unittest.skipUnless(_TMUX_OK, "needs tmux 3.1+")
class _TmuxTest(unittest.TestCase):
    def setUp(self) -> None:
        # Short root: Unix socket paths are limited to ~104 bytes on macOS.
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbt.")))
        self.addCleanup(use_bottle_root(root))
        self.addCleanup(lambda: subprocess.run(sessions.tmux("kill-server"), capture_output=True,
                                               check=False))
        self.finished: list[tuple[str, sessions.ExitStatus]] = []
        self.manager = self._manager()
        self.local = localsock.serve(sessions.control_socket_path(), self.manager)
        self.addCleanup(self.local.close)

    def _manager(self) -> sessions.SessionManager:
        return sessions.SessionManager(
            {"test": lambda rec, ex: self.finished.append((rec.id, ex))})

    def start(self, session_id: str, script: str, *, writable: bool = False,
              env: dict[str, str] | None = None) -> None:
        self.manager.start(
            sessions.SessionRecord(id=session_id, kind="test", writable_allowed=writable),
            ["sh", "-c", script], env or {})

    def pane(self, session_id: str) -> str:
        return subprocess.run(sessions.tmux("capture-pane", "-p", "-t", f"={session_id}:"),
                              capture_output=True, text=True, check=False).stdout


class TestSessions(_TmuxTest):
    def test_exit_status_reaches_the_finisher_and_the_session_goes(self) -> None:
        self.start("s1", "exit 7")
        self.assertEqual(sessions.ExitStatus(7), self.manager.wait_exit("s1", timeout=10))
        self.assertTrue(_until(lambda: not self.manager.exists("s1")))
        self.assertEqual([("s1", sessions.ExitStatus(7))], self.finished)
        self.assertIsNone(self.manager.record("s1"))

    def test_secrets_stay_out_of_tmux_and_specs_are_handed_out_once(self) -> None:
        self.start("s2", 'echo "probe=$PROBE"; sleep 30', env={"PROBE": "dummy-not-a-secret"})
        self.assertTrue(_until(lambda: "probe=dummy-not-a-secret" in self.pane("s2")))
        tmux_env = subprocess.run(sessions.tmux("show-environment", "-g"),
                                  capture_output=True, text=True, check=False).stdout
        self.assertNotIn("dummy-not-a-secret", tmux_env)
        self.assertIsNone(self.manager.spec("s2"))

    def test_rejects_bad_ids_and_duplicates(self) -> None:
        with self.assertRaises(sessions.SessionError):
            self.start("Bad ID", "true")
        self.start("s3", "sleep 30")
        with self.assertRaises(sessions.SessionError):
            self.start("s3", "sleep 30")

    def test_adopt_finishes_sessions_that_died_while_the_daemon_was_down(self) -> None:
        self.start("gone", "echo UP; sleep 1; exit 3")
        self.assertTrue(_until(lambda: "UP" in self.pane("gone")))
        self.local.close()  # the daemon is "down": the hook can't reach it
        time.sleep(2)
        subprocess.run(sessions.tmux("new-session", "-d", "-s", "stranger", "sleep 30"),
                       capture_output=True, check=False)
        (sessions.control_socket_path().parent / "sessions" / "stale.json").write_text(
            json.dumps({"id": "stale", "kind": "test"}))
        manager = self._manager()
        with _quiet():
            manager.adopt()
        self.assertEqual([("gone", sessions.ExitStatus(3))], self.finished)
        self.assertFalse(manager.exists("gone"))
        self.assertTrue(manager.exists("stranger"))
        self.assertIsNone(manager.record("stale"))


def _quiet() -> contextlib.redirect_stderr[io.StringIO]:
    """Swallow the warnings adopt prints for unknown sessions."""
    return contextlib.redirect_stderr(io.StringIO())


class _Received:
    def __init__(self) -> None:
        self.bytes = bytearray()
        self.text: list[dict[str, object]] = []


class TestAttachRoute(_TmuxTest):
    def setUp(self) -> None:
        super().setUp()
        self.key = "k" * 32
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key, self.manager)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def connect(self, session_id: str):  # type: ignore[no-untyped-def]
        sock, reader, writer = ws.connect(
            "127.0.0.1", self.port, f"/sessions/{session_id}/attach?cols=90&rows=20",
            bearer(HOST_CONTROL.mint(ROLE_CLI, self.key)))
        self.addCleanup(sock.close)
        received = _Received()

        def read() -> None:
            try:
                while True:
                    op, data = ws.read_frame(reader)
                    if op == ws.BINARY:
                        received.bytes.extend(data)
                    elif op == ws.TEXT:
                        received.text.append(json.loads(data))
                    elif op == ws.CLOSE:
                        return
            except (EOFError, OSError):
                return

        threading.Thread(target=read, daemon=True).start()
        return writer, received

    def send(self, writer, opcode: int, payload: bytes) -> None:  # type: ignore[no-untyped-def]
        ws.write_frame(writer, opcode, payload, mask=True)

    def test_read_only_then_writable_then_exit(self) -> None:
        self.start("a1", "echo READY; exec cat", writable=True)
        self.assertTrue(_until(lambda: "READY" in self.pane("a1")))
        writer, received = self.connect("a1")
        self.assertTrue(_until(lambda: b"READY" in received.bytes))
        self.send(writer, ws.BINARY, b"dropped\r")
        time.sleep(0.5)
        self.assertNotIn("dropped", self.pane("a1"))
        self.send(writer, ws.TEXT, json.dumps({"type": "writable", "value": True}).encode())
        time.sleep(0.3)
        self.send(writer, ws.BINARY, b"typed\r")
        # Typed once by the terminal's echo and once by cat: the line was read.
        self.assertTrue(_until(lambda: self.pane("a1").count("typed") >= 2))
        self.send(writer, ws.BINARY, b"\x04")  # EOF to cat: the agent exits 0
        self.assertTrue(_until(lambda: received.text))
        self.assertEqual({"type": "exit", "status": 0, "signal": ""}, received.text[0])

    def test_writable_is_refused_when_the_agent_does_not_allow_it(self) -> None:
        self.start("a2", "echo READY; exec cat", writable=False)
        self.assertTrue(_until(lambda: "READY" in self.pane("a2")))
        writer, received = self.connect("a2")
        self.assertTrue(_until(lambda: b"READY" in received.bytes))
        self.send(writer, ws.TEXT, json.dumps({"type": "writable", "value": True}).encode())
        time.sleep(0.3)
        self.send(writer, ws.BINARY, b"nope\r")
        time.sleep(0.5)
        self.assertNotIn("nope", self.pane("a2"))

    def test_rejects_missing_token_plain_requests_and_unknown_sessions(self) -> None:
        self.start("a3", "sleep 30")
        with self.assertRaises(ws.WebSocketError):
            ws.connect("127.0.0.1", self.port, "/sessions/a3/attach")
        with self.assertRaises(ws.WebSocketError):
            ws.connect("127.0.0.1", self.port, "/sessions/missing/attach",
                       bearer(HOST_CONTROL.mint(ROLE_CLI, self.key)))


if __name__ == "__main__":
    unittest.main()
