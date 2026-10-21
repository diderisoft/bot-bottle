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
from bot_bottle.contract.auth import CONTROL_PLANE, ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR, SERVICE_NAME, bearer
from bot_bottle.control.hostctl import runner
from bot_bottle.control.hostctl.build import build_id
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.trust_domain import signing_key

from . import use_bottle_root


class TestServer(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HostControlServer(("127.0.0.1", 0), "build-x", "k" * 32)
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


class TestAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(use_bottle_root(Path(self.enterContext(tempfile.TemporaryDirectory()))))
        self.key = signing_key(HOST_CONTROL)
        self.server = HostControlServer(("127.0.0.1", 0), "b", self.key)
        self.addCleanup(self.server.server_close)

    def test_roles_from_bearer_tokens(self) -> None:
        for role in (ROLE_CLI, ROLE_ORCHESTRATOR, ROLE_GATEWAY):
            header = bearer(HOST_CONTROL.mint(role, self.key))["Authorization"]
            self.assertEqual(role, self.server.role_of(header))

    def test_rejects_other_keys_domains_and_malformed_headers(self) -> None:
        self.assertIsNone(self.server.role_of(None))
        self.assertIsNone(self.server.role_of("Bearer"))
        self.assertIsNone(self.server.role_of("Basic abc"))
        other = HOST_CONTROL.mint(ROLE_CLI, "x" * 32)
        self.assertIsNone(self.server.role_of(f"Bearer {other}"))
        control_plane = CONTROL_PLANE.mint(ROLE_CLI, signing_key(CONTROL_PLANE))
        self.assertIsNone(self.server.role_of(f"Bearer {control_plane}"))

    def test_own_key_file_and_cli_token(self) -> None:
        self.assertNotEqual(HOST_CONTROL.key_filename, CONTROL_PLANE.key_filename)
        self.assertNotEqual(self.key, signing_key(CONTROL_PLANE))
        self.assertEqual(ROLE_CLI, self.server.role_of(bearer(runner.cli_token())["Authorization"]))

    def test_refuses_to_start_without_a_key(self) -> None:
        with self.assertRaises(ValueError):
            HostControlServer(("127.0.0.1", 0), "b", "")


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

    def test_a_daemon_holding_bottles_is_kept_and_only_force_stops_it(self) -> None:
        first = runner.ensure_running()
        self.assertEqual([], runner.held_bottles(first))  # the real route: nothing held
        with mock.patch.object(runner, "held_bottles", return_value=["b1"]), \
             mock.patch("sys.stderr", io.StringIO()):
            with mock.patch.object(runner, "current_build_id", return_value="other-code"):
                self.assertEqual(first, runner.ensure_running())
            with self.assertRaises(runner.HostControlError) as cm:
                runner.stop()
            self.assertIn("b1", str(cm.exception))
            self.assertTrue(runner.probe(first))
            self.assertTrue(runner.stop(force=True))
        self.assertIsNone(runner.running())


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
