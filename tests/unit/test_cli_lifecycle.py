"""Unit: the host-service commands (`install`, `up`, `down`,
`status`, `jobs`). The daemon, backends and service manager are mocked."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from bot_bottle.cli.commands import lifecycle
from bot_bottle.control.hostctl.runner import HostControlState

_STATE = HostControlState(pid=7, port=1234, build_id="abc")


class _Mocked(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = mock.MagicMock()
        self.backend.name = "firecracker"
        self.backend.setup.return_value = 0
        self.backend.teardown.return_value = 0
        self.backend.infra_service.return_value.is_healthy.return_value = True
        self.backend.infra_service.return_value.url.return_value = "http://orch:8099"
        self.enterContext(
            mock.patch.object(lifecycle, "get_bottle_backend", return_value=self.backend))
        self.runner = self.enterContext(mock.patch.object(lifecycle, "runner"))
        self.runner.HostControlError = RuntimeError
        self.runner.running.return_value = _STATE
        self.runner.held_bottles.return_value = []
        self.runner.stop.return_value = True
        self.runner.ensure_running.return_value = _STATE
        self.service = self.enterContext(mock.patch.object(lifecycle, "service_unit"))
        self.service.installed.return_value = False

    def run_cmd(self, fn, argv: list[str]) -> tuple[int, str, str]:  # type: ignore[no-untyped-def]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argv)
        return code, out.getvalue(), err.getvalue()


class TestUpDown(_Mocked):
    def test_up_and_up_restart(self) -> None:
        with mock.patch.object(lifecycle, "ensure_via_daemon",
                               return_value="http://orch") as ensure:
            code, out, _ = self.run_cmd(lifecycle.cmd_up, [])
            self.assertEqual(0, code)
            self.assertFalse(ensure.call_args.kwargs["restart"])
            self.runner.restart.assert_not_called()
            self.run_cmd(lifecycle.cmd_up, ["--restart"])
            self.assertTrue(ensure.call_args.kwargs["restart"])
        self.assertIn("firecracker orchestrator: running at http://orch", out)
        # #679: --restart restarts the host controller too, asking first.
        self.runner.restart.assert_called_once_with(
            confirm_restart=lifecycle.confirm_controller_restart)

    def test_up_restart_declined_fails(self) -> None:
        self.runner.restart.side_effect = RuntimeError("not restarting; 1 bottle(s) are running")
        with mock.patch.object(lifecycle, "ensure_via_daemon") as ensure:
            code, _, err = self.run_cmd(lifecycle.cmd_up, ["--restart"])
        self.assertEqual(1, code)
        self.assertIn("not restarting", err)
        ensure.assert_not_called()

    def test_down_stops_infra_then_the_daemon(self) -> None:
        code, out, _ = self.run_cmd(lifecycle.cmd_down, [])
        self.assertEqual(0, code)
        self.backend.infra_service.return_value.stop.assert_called_once()
        self.runner.stop.assert_called_once_with(force=False)
        self.assertIn("host controller: stopped", out)

    def test_down_refuses_while_bottles_run_unless_forced(self) -> None:
        self.runner.held_bottles.return_value = ["b1"]
        code, _, err = self.run_cmd(lifecycle.cmd_down, [])
        self.assertEqual(1, code)
        self.assertIn("b1", err)
        self.backend.infra_service.return_value.stop.assert_not_called()
        self.assertEqual(0, self.run_cmd(lifecycle.cmd_down, ["--force"])[0])
        self.runner.stop.assert_called_with(force=True)


class TestStatusJobs(_Mocked):
    def test_status_reports_each_part(self) -> None:
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[object()]):
            code, out, _ = self.run_cmd(lifecycle.cmd_status, [])
        self.assertEqual(0, code)
        self.assertIn("host controller: running at http://127.0.0.1:1234", out)
        self.assertIn("autostart: not installed", out)
        self.assertIn("firecracker orchestrator: running at http://orch:8099", out)
        self.assertIn("bottled agents: 1 running", out)

    def test_status_exits_1_when_something_is_down(self) -> None:
        self.runner.running.return_value = None
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            code, out, _ = self.run_cmd(lifecycle.cmd_status, [])
        self.assertEqual(1, code)
        self.assertIn("host controller: not running", out)

    def test_jobs(self) -> None:
        client = mock.MagicMock()
        client.jobs.return_value = [{"id": "j1", "kind": "bottle.launch", "state": "succeeded"}]
        with mock.patch.object(lifecycle, "HostControlClient", return_value=client):
            code, out, _ = self.run_cmd(lifecycle.cmd_jobs, [])
        self.assertEqual((0, "j1\tbottle.launch\tsucceeded\n"), (code, out))
        self.runner.running.return_value = None
        self.assertEqual(1, self.run_cmd(lifecycle.cmd_jobs, [])[0])
        self.assertEqual(2, self.run_cmd(lifecycle.cmd_jobs, ["bogus"])[0])


class TestInstallUninstall(_Mocked):
    def test_install_needs_tmux_then_sets_up_and_installs_autostart(self) -> None:
        with mock.patch.object(lifecycle, "tmux_version", return_value=None):
            self.assertEqual(1, self.run_cmd(lifecycle.cmd_install, [])[0])
        self.backend.setup.assert_not_called()
        with mock.patch.object(lifecycle, "tmux_version", return_value=(3, 4)):
            code, out, _ = self.run_cmd(lifecycle.cmd_install, [])
        self.assertEqual(0, code)
        self.backend.setup.assert_called_once()
        self.service.install.assert_called_once()
        self.assertIn("host controller: running", out)

    def test_install_without_autostart_and_a_failed_setup(self) -> None:
        with mock.patch.object(lifecycle, "tmux_version", return_value=(3, 4)):
            self.assertEqual(0, self.run_cmd(lifecycle.cmd_install, ["--no-autostart"])[0])
            self.service.install.assert_not_called()
            self.backend.setup.return_value = 3
            self.assertEqual(3, self.run_cmd(lifecycle.cmd_install, [])[0])


if __name__ == "__main__":
    unittest.main()
