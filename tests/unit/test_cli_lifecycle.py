"""Unit: the host-service commands (`install`, `up`, `down`, `status`, the
unlisted `jobs`) and `bootstrap`. The daemon, backends and service manager
are mocked."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from bot_bottle.cli.commands import doctor, lifecycle
from bot_bottle.backend.docker.infra import GATEWAY_NAME, ORCHESTRATOR_NAME
from bot_bottle.control.client import OrchestratorEndpoint
from bot_bottle.environment import active_name
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
        self.runner.restart_needed.return_value = False
        self.report = self.enterContext(mock.patch.object(
            doctor, "orchestrator_report", return_value=doctor.OrchestratorReport(
                OrchestratorEndpoint("firecracker", "http://orch:8099"), True, ())))
        self.service = self.enterContext(mock.patch.object(lifecycle, "service_unit"))
        self.service.installed.return_value = False
        # Not root (CI runs as root): `install` under sudo does only the
        # privileged part.
        self.enterContext(mock.patch.object(lifecycle.os, "geteuid", return_value=501))

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
            # --restart restarts the host controller; a current infra pair
            # is adopted, not rebooted (#879).
            self.run_cmd(lifecycle.cmd_up, ["--restart"])
            self.assertFalse(ensure.call_args.kwargs["restart"])
            self.run_cmd(lifecycle.cmd_up, ["--restart-infra"])
            self.assertTrue(ensure.call_args.kwargs["restart"])
        self.assertIn("firecracker orchestrator: running at http://orch", out)
        # #679: --restart restarts the host controller too, asking first.
        self.runner.restart.assert_called_once_with(
            confirm_restart=lifecycle.confirm_controller_restart)

    def test_up_passes_the_reattach_override(self) -> None:
        with mock.patch.object(lifecycle, "ensure_via_daemon", return_value="http://orch") as ensure:
            self.run_cmd(lifecycle.cmd_up, [])
            self.assertFalse(ensure.call_args.kwargs["allow_reattach_failures"])
            self.run_cmd(lifecycle.cmd_up, ["--allow-reattach-failures"])
            self.assertTrue(ensure.call_args.kwargs["allow_reattach_failures"])

    def test_up_restart_yes_doesnt_ask(self) -> None:
        # For scripts and services, with no terminal to answer the question.
        with mock.patch.object(lifecycle, "ensure_via_daemon", return_value="http://orch"):
            self.assertEqual(0, self.run_cmd(lifecycle.cmd_up, ["--restart", "--yes"])[0])
        confirm = self.runner.restart.call_args.kwargs["confirm_restart"]
        self.assertTrue(confirm(["b1"]))

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

    def test_status_says_when_the_host_controller_predates_an_upgrade(self) -> None:
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            out = self.run_cmd(lifecycle.cmd_status, [])[1]
        self.assertNotIn("restart needed", out)
        self.runner.restart_needed.return_value = True
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            out = self.run_cmd(lifecycle.cmd_status, [])[1]
        self.assertIn("host controller: restart needed (installed version changed): run "
                      "`bot-bottle up --restart`; running bottles keep running", out)
        self.runner.restart_needed.assert_called_with(_STATE)

    def test_status_reports_the_environments_orchestrator_and_warns_of_strays(self) -> None:
        self.report.return_value = doctor.OrchestratorReport(
            OrchestratorEndpoint("firecracker", "http://orch:8099"), True,
            (OrchestratorEndpoint("docker", "http://127.0.0.1:8099"),))
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            code, out, _ = self.run_cmd(lifecycle.cmd_status, ["--backend", "docker"])
        self.assertEqual(0, code)
        self.assertIn("firecracker orchestrator: running at http://orch:8099", out)
        self.assertIn("warning: another bot-bottle orchestrator (docker) answers at "
                      f"http://127.0.0.1:8099; it isn't environment {active_name()}'s", out)
        self.assertIn(f"docker rm -f {ORCHESTRATOR_NAME} {GATEWAY_NAME}", out)

    def test_status_without_a_recorded_orchestrator_says_so(self) -> None:
        self.report.return_value = doctor.OrchestratorReport(None, False, ())
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            code, out, _ = self.run_cmd(lifecycle.cmd_status, [])
        self.assertEqual(1, code)
        self.assertIn(f"orchestrator: no orchestrator for environment {active_name()}; "
                      "run `bot-bottle up`", out)

    def test_status_exits_1_when_something_is_down(self) -> None:
        self.runner.running.return_value = None
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]):
            code, out, _ = self.run_cmd(lifecycle.cmd_status, [])
        self.assertEqual(1, code)
        self.assertIn("host controller: not running", out)

    def test_status_shows_the_console_link_and_jobs_on_request(self) -> None:
        client = mock.MagicMock()
        client.jobs.return_value = [{"id": "j1", "kind": "bottle.launch", "state": "succeeded"}]
        with mock.patch.object(lifecycle, "enumerate_active_agents", return_value=[]), \
                mock.patch("bot_bottle.cli.commands.remote.link_line",
                           return_value="console link: off"), \
                mock.patch.object(lifecycle, "HostControlClient", return_value=client):
            _, out, _ = self.run_cmd(lifecycle.cmd_status, [])
            self.assertIn("console link: off", out)
            self.assertNotIn("j1", out)
            _, out, _ = self.run_cmd(lifecycle.cmd_status, ["--jobs"])
        self.assertIn("j1\tbottle.launch\tsucceeded", out)

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



class TestInstallUndoAndSudo(_Mocked):
    def test_undo_tears_the_backend_down_only(self) -> None:
        code, _, _ = self.run_cmd(lifecycle.cmd_install, ["--undo"])
        self.assertEqual(0, code)
        self.backend.teardown.assert_called_once()
        self.backend.setup.assert_not_called()
        self.service.install.assert_not_called()

    def test_under_sudo_only_the_privileged_part(self) -> None:
        with mock.patch.object(lifecycle.os, "geteuid", return_value=0), \
                mock.patch.object(lifecycle, "tmux_version", return_value=None):
            self.assertEqual(0, self.run_cmd(lifecycle.cmd_install, [])[0])
        self.backend.setup.assert_called_once()
        self.service.install.assert_not_called()


class TestInstallOffersSudo(_Mocked):
    """`install` on a terminal offers to run the backend's root step under
    sudo, with the user's say-so; without one it prints the commands."""

    def setUp(self) -> None:
        super().setUp()

        def which(name: str) -> str:
            return f"/usr/bin/{name}"

        self.backend.root_setup.return_value = "install the network pool"
        self.enterContext(mock.patch.object(lifecycle, "tmux_version", return_value=(3, 4)))
        self.enterContext(mock.patch.object(lifecycle.shutil, "which",
                                            side_effect=which))
        self.tty = self.enterContext(mock.patch.object(lifecycle.sys.stdin, "isatty",
                                                       return_value=True))
        self.sudo = self.enterContext(mock.patch.object(lifecycle.subprocess, "run"))
        self.sudo.return_value.returncode = 0

    def install(self, answer: str) -> tuple[int, str]:
        with mock.patch("builtins.input", return_value=answer):
            code, _, err = self.run_cmd(lifecycle.cmd_install, [])
        return code, err

    def test_yes_runs_it_under_sudo_then_autostart(self) -> None:
        with mock.patch.dict(lifecycle.os.environ, {"BOT_BOTTLE_ENV": "demo"}):
            code, err = self.install("y")
        self.assertEqual(0, code)
        argv = self.sudo.call_args.args[0]
        self.assertEqual(["sudo", "env", "BOT_BOTTLE_ENV=demo"], argv[:3])
        self.assertEqual(["install", "--backend=firecracker"], argv[-2:])
        self.assertIn("needs root once, to install the network pool", err)
        self.backend.setup.assert_not_called()  # the root run did it
        self.service.install.assert_called_once()

    def test_no_prints_the_commands_as_before(self) -> None:
        code, err = self.install("")
        self.assertEqual(0, code)
        self.sudo.assert_not_called()
        self.backend.setup.assert_called_once()
        self.assertIn("here's what to run yourself", err)

    def test_no_answer_at_all_is_no(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            code, _, err = self.run_cmd(lifecycle.cmd_install, [])
        self.assertEqual(0, code)
        self.sudo.assert_not_called()
        self.assertIn("here's what to run yourself", err)

    def test_a_failed_sudo_run_stops_install(self) -> None:
        self.sudo.return_value.returncode = 1
        self.assertEqual(1, self.install("yes")[0])
        self.service.install.assert_not_called()

    def test_nothing_to_ask_or_no_one_to_ask(self) -> None:
        self.backend.root_setup.return_value = None
        self.install("y")
        self.backend.root_setup.return_value = "x"
        self.tty.return_value = False
        self.install("y")
        self.sudo.assert_not_called()
        self.assertEqual(2, self.backend.setup.call_count)


class TestBootstrap(unittest.TestCase):
    def test_install_setup_doctor_in_order_stopping_at_a_failure(self) -> None:
        from bot_bottle.cli.commands import bootstrap  # pylint: disable=import-outside-toplevel
        calls: list[tuple[str, list[str]]] = []

        def step(name: str, code: int = 0):  # type: ignore[no-untyped-def]
            def run(argv: list[str]) -> int:
                calls.append((name, argv))
                return code
            return run

        with mock.patch("bot_bottle.cli.commands.lifecycle.cmd_install", step("install")), \
                mock.patch("bot_bottle.cli.commands.setup.cmd_setup", step("setup")), \
                mock.patch("bot_bottle.cli.commands.doctor.cmd_doctor", step("doctor")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(0, bootstrap.cmd_bootstrap(["--backend", "docker", "--no-pair"]))
        self.assertEqual([("install", ["--backend", "docker"]), ("setup", ["--no-pair"]),
                          ("doctor", [])], calls)
        calls.clear()
        err = io.StringIO()
        with mock.patch("bot_bottle.cli.commands.lifecycle.cmd_install", step("install", 3)), \
                mock.patch("bot_bottle.cli.commands.setup.cmd_setup", step("setup")), \
                redirect_stderr(err):
            self.assertEqual(3, bootstrap.cmd_bootstrap([]))
        self.assertEqual(["install"], [c[0] for c in calls])
        self.assertIn("run `bot-bottle bootstrap` again", err.getvalue())


if __name__ == "__main__":
    unittest.main()
