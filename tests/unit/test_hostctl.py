"""Unit: the host controller daemon skeleton (PRD 0092 chunk 1)."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands.hostctl import cmd_hostctl
from bot_bottle.contract.hostctl import SERVICE_NAME
from bot_bottle.control.hostctl import runner
from bot_bottle.control.hostctl.build import build_id
from bot_bottle.control.hostctl.server import HostControlServer

from . import use_bottle_root


class TestServer(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HostControlServer(("127.0.0.1", 0), "build-x")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_status_reports_service_build_and_pid(self) -> None:
        with urllib.request.urlopen(self.url + "/status", timeout=5) as resp:
            body = json.loads(resp.read())
        self.assertEqual(SERVICE_NAME, body["service"])
        self.assertEqual("build-x", body["build_id"])
        self.assertIsInstance(body["pid"], int)

    def test_unknown_route_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/nope", timeout=5)
        self.assertEqual(404, cm.exception.code)


class TestBuildId(unittest.TestCase):
    def test_stable_and_sensitive_to_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "a.py").write_text("ignored\n")
            first = build_id(root)
            self.assertEqual(first, build_id(root))
            (root / "a.py").write_text("x = 2\n")
            self.assertNotEqual(first, build_id(root))


class TestState(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(use_bottle_root(Path(self.enterContext(tempfile.TemporaryDirectory()))))

    def test_round_trip_and_clear_only_own_state(self) -> None:
        state = runner.HostControlState(pid=123, port=4567, build_id="b")
        runner.write_state(state)
        self.assertEqual(state, runner.read_state())
        runner.clear_state(999)
        self.assertEqual(state, runner.read_state())
        runner.clear_state(123)
        self.assertIsNone(runner.read_state())

    def test_unreadable_state_is_none(self) -> None:
        runner.state_dir().mkdir(parents=True)
        (runner.state_dir() / runner.STATE_FILENAME).write_text("{not json")
        self.assertIsNone(runner.read_state())

    def test_state_for_a_dead_daemon_is_not_running(self) -> None:
        runner.write_state(runner.HostControlState(pid=1, port=1, build_id="b"))
        self.assertIsNone(runner.running())


class TestEnsureRunning(unittest.TestCase):
    """Spawns real daemons under an isolated bot-bottle root."""

    def setUp(self) -> None:
        self.addCleanup(use_bottle_root(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.addCleanup(runner.stop)

    def test_starts_reuses_restarts_on_new_code_and_stops(self) -> None:
        first = runner.ensure_running()
        self.assertTrue(runner.probe(first))
        self.assertEqual(first, runner.ensure_running())

        with mock.patch.object(runner, "current_build_id", return_value="other-code"):
            second = runner.ensure_running()
        self.assertNotEqual(first.pid, second.pid)
        self.assertFalse(runner.probe(first))

        self.assertTrue(runner.stop())
        self.assertIsNone(runner.running())
        self.assertIsNone(runner.read_state())
        self.assertFalse(runner.stop())


class TestCommand(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(use_bottle_root(Path(self.enterContext(tempfile.TemporaryDirectory()))))

    def test_bad_usage_exits_2(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, cmd_hostctl([]))
            self.assertEqual(2, cmd_hostctl(["bogus"]))

    def test_status_when_not_running_exits_1(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(1, cmd_hostctl(["status"]))
        self.assertIn("not running", out.getvalue())


if __name__ == "__main__":
    unittest.main()
