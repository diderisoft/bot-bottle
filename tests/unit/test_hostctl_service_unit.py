"""Unit: the optional user service for the host controller (PRD 0092 chunk 9).
Nothing here touches the real service manager: commands are captured."""

from __future__ import annotations

import io
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands import backend as backend_cmd
from bot_bottle.control.hostctl import runner, service_unit

from . import use_bottle_root

_SPEC = service_unit.ServiceSpec(python="/usr/bin/python3", path_env="/opt/bin:/usr/bin")


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(use_bottle_root(self.root))
        self.calls: list[list[str]] = []

        def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.enterContext(mock.patch.object(service_unit.subprocess, "run", side_effect=run))

    def use(self, kind: str) -> None:
        self.enterContext(mock.patch.object(service_unit, "manager", return_value=kind))


class TestRendering(_Isolated):
    def test_launchd_runs_the_daemon_with_the_clis_path_and_root(self) -> None:
        plist = plistlib.loads(service_unit.render_launchd(_SPEC))
        self.assertEqual(service_unit.LAUNCHD_LABEL, plist["Label"])
        self.assertEqual(["/usr/bin/python3", "-m", "bot_bottle.control.hostctl"],
                         plist["ProgramArguments"])
        self.assertEqual("/opt/bin:/usr/bin", plist["EnvironmentVariables"]["PATH"])
        self.assertEqual(str(self.root), plist["EnvironmentVariables"]["BOT_BOTTLE_ROOT"])
        self.assertEqual({"SuccessfulExit": False}, plist["KeepAlive"])

    def test_systemd_quotes_and_restarts_only_on_failure(self) -> None:
        unit = service_unit.render_systemd(_SPEC)
        self.assertIn('ExecStart="/usr/bin/python3" "-m" "bot_bottle.control.hostctl"', unit)
        self.assertIn(f'Environment="BOT_BOTTLE_ROOT={self.root}"', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)


class TestInstall(_Isolated):
    def test_launchd_install_uninstall_and_root_matching(self) -> None:
        self.use("launchd")
        self.assertFalse(service_unit.installed())
        path = service_unit.install(_SPEC)
        self.assertTrue(path.is_file())
        self.assertTrue(service_unit.installed())
        self.assertTrue(any(c[:2] == ["launchctl", "bootstrap"] and c[-1] == str(path)
                            for c in self.calls))
        with mock.patch.object(service_unit, "bot_bottle_root", return_value=Path("/other")):
            self.assertFalse(service_unit.installed())
        service_unit.start()
        self.assertEqual("kickstart", self.calls[-1][1])
        self.assertTrue(service_unit.uninstall())
        self.assertFalse(path.exists())
        self.assertFalse(service_unit.uninstall())

    def test_systemd_install_uninstall(self) -> None:
        self.use("systemd")
        path = service_unit.install(_SPEC)
        self.assertTrue(service_unit.installed())
        self.assertIn(["systemctl", "--user", "enable", "--now", service_unit.SYSTEMD_UNIT],
                      self.calls)
        service_unit.start()
        self.assertEqual(["systemctl", "--user", "start", service_unit.SYSTEMD_UNIT],
                         self.calls[-1])
        self.assertTrue(service_unit.uninstall())
        self.assertFalse(path.exists())

    def test_a_failing_manager_command_raises(self) -> None:
        self.use("systemd")
        with mock.patch.object(service_unit.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, "", "nope")):
            with self.assertRaises(runner.HostControlError):
                service_unit.install(_SPEC)


class TestRunnerAndCli(_Isolated):
    def test_runner_starts_through_the_service_when_installed(self) -> None:
        state = runner.HostControlState(pid=1, port=2, build_id="b")
        with mock.patch.object(service_unit, "installed", return_value=True), \
             mock.patch.object(service_unit, "start") as start, \
             mock.patch.object(runner, "_await_state", return_value=state) as wait:
            self.assertEqual(state, runner._start(5))  # pyright: ignore[reportPrivateUsage]
        start.assert_called_once()
        self.assertIsNone(wait.call_args.args[0])

    def test_backend_setup_hint(self) -> None:
        self.use("launchd")
        with mock.patch("sys.stderr", io.StringIO()) as err:
            backend_cmd._note_service()  # pyright: ignore[reportPrivateUsage]
        self.assertIn("install` also sets up autostart", err.getvalue())


if __name__ == "__main__":
    unittest.main()
