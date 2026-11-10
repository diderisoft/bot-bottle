"""Unit: the host controller daemon skeleton (PRD 0092 chunk 1)."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.contract.auth import CONTROL_PLANE, ROLE_CLI, ROLE_GATEWAY
from bot_bottle.contract.hostctl import HOST_CONTROL, ROLE_ORCHESTRATOR, SERVICE_NAME, bearer
from bot_bottle.control.hostctl import host_db, localsock, runner
from bot_bottle.control.hostctl.build import build_id
from bot_bottle.control.hostctl.server import HostControlServer
from bot_bottle.control.hostctl.sessions import SessionManager
from bot_bottle.paths import host_db_path
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


class TestDiscovery(unittest.TestCase):
    """The CLI finds the daemon on its control socket, at a fixed path, and
    asks it for its pid, port and build id. There is no state file."""

    def setUp(self) -> None:
        # A short path: a Unix socket's must fit in 108 bytes.
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbd.")))
        self.addCleanup(use_bottle_root(root))
        self.status: dict[str, object] = {}

    def serve(self) -> None:
        server = localsock.serve(runner.control_socket_path(), SessionManager({}),
                                 lambda: dict(self.status) or None)
        self.addCleanup(server.close)

    def test_the_socket_is_at_a_fixed_path_under_the_root(self) -> None:
        self.assertEqual(runner.state_dir() / "control.sock", runner.control_socket_path())
        self.serve()
        self.assertEqual(0o600, stat.S_IMODE(runner.control_socket_path().stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(runner.state_dir().stat().st_mode))

    def test_reads_the_daemons_status(self) -> None:
        self.serve()
        self.status.update(pid=123, port=4567, build_id="b")
        self.assertEqual(runner.HostControlState(pid=123, port=4567, build_id="b"),
                         runner.read_state())
        self.assertEqual(runner.read_state(), runner.running())
        # Another environment's daemon, by its state dir.
        self.assertEqual(runner.read_state(), runner.read_state(runner.state_dir()))

    def test_a_daemon_still_starting_is_not_running_yet(self) -> None:
        self.serve()
        self.assertIsNone(runner.read_state())

    def test_no_daemon_is_not_running(self) -> None:
        self.assertIsNone(runner.running())
        # A socket file left by a daemon that's gone.
        self.serve()
        self.status.update(pid=1, port=1, build_id="b")
        runner.control_socket_path().unlink()
        self.assertIsNone(runner.running())


class TestEnsureRunning(unittest.TestCase):
    """Spawns real daemons under an isolated bot-bottle root."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbr.")))
        self.addCleanup(use_bottle_root(root))
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
        self.assertEqual([], list(runner.state_dir().glob("*.json")))

    def test_the_daemon_migrates_the_host_db_as_it_starts(self) -> None:
        runner.ensure_running()
        self.assertTrue(all(store.is_migrated() for store in host_db.stores()))

    def test_a_corrupt_host_db_stops_the_start_naming_the_backup(self) -> None:
        runner.ensure_running()
        runner.stop()
        backup = host_db.back_up()
        with host_db_path().open("r+b") as fh:
            fh.write(b"\xff" * 100)  # over the header
        with self.assertRaises(runner.HostControlError) as cm:
            runner.ensure_running()
        self.assertIn(f"exited with {runner.REFUSED_EXIT_STATUS}", str(cm.exception))
        self.assertIn(backup.name, str(cm.exception))
        self.assertIn("host restore-db", str(cm.exception))
        self.assertIsNone(runner.running())

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

    def test_a_launch_restarts_an_outdated_daemon_holding_bottles_only_if_confirmed(self) -> None:
        # #679: its bottles keep running and the new daemon adopts them.
        first = runner.ensure_running()
        asked: list[list[str]] = []

        def confirm(answer: bool) -> runner.ConfirmRestart:
            def ask(held: list[str]) -> bool:
                asked.append(held)
                return answer
            return ask

        with mock.patch.object(runner, "held_bottles", return_value=["b1"]), \
             mock.patch.object(runner, "current_build_id", return_value="other-code"), \
             mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(runner.StaleHostControllerError) as cm:
                runner.ensure_running(confirm_restart=confirm(False))
            self.assertIn("up --restart", str(cm.exception))
            self.assertTrue(runner.probe(first))
            second = runner.ensure_running(confirm_restart=confirm(True))
        self.assertEqual([["b1"], ["b1"]], asked)
        self.assertNotEqual(first.pid, second.pid)
        self.assertFalse(runner.probe(first))

    def test_restart_replaces_the_daemon_even_on_the_same_code(self) -> None:
        first = runner.ensure_running()
        with mock.patch("sys.stderr", io.StringIO()):
            second = runner.restart(confirm_restart=lambda _held: False)  # nothing held: no ask
        self.assertNotEqual(first.pid, second.pid)
        with mock.patch.object(runner, "held_bottles", return_value=["b1"]), \
             mock.patch("sys.stderr", io.StringIO()), \
             self.assertRaises(runner.StaleHostControllerError):
            runner.restart(confirm_restart=lambda _held: False)
        self.assertTrue(runner.probe(second))


if __name__ == "__main__":
    unittest.main()
