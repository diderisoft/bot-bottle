"""Unit: `bot-bottle doctor` host prerequisite checks (ADR 0004).

`doctor` is a store-free diagnostic — it must run on a fresh install
before any DB migration, and its exit code gates only the two hard
prerequisites (Python and at least one *ready* backend). The config-dir
check is advisory and never affects the exit code.

Backend readiness is probed with `is_backend_ready()` (a full status()
check), not the cheap PATH-only `is_backend_available()` — a host with a
stopped daemon or half-configured backend must not report `ok`.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.commands import doctor
from bot_bottle.control.client import OrchestratorEndpoint
from bot_bottle.control.hostctl.runner import HostControlState


def _run(argv: list[str] | None = None, selected: str = "") -> tuple[int, str]:
    """doctor's output. `selected` is the backend a launch would use; "" (none
    selectable) keeps the any-ready-backend rule."""
    buf = io.StringIO()
    with redirect_stdout(buf), patch.object(doctor, "selected_backend", return_value=selected):
        code = doctor.cmd_doctor(argv or [])
    return code, buf.getvalue()


_FC = OrchestratorEndpoint("firecracker", "http://10.243.255.1:8099")
_STRAY = OrchestratorEndpoint("docker", "http://127.0.0.1:8099")


def _only_docker_ready(_settings: object, name: str, *, quiet: bool = False) -> bool:
    del quiet
    return name == "docker"


class TestDoctor(unittest.TestCase):
    def setUp(self) -> None:
        # Every backend runs here, and Docker has no gVisor, unless a test says
        # otherwise; neither touches the real host.
        self.os_req = self.enterContext(
            patch.object(doctor, "backend_os_requirement", return_value=""))
        self.enterContext(patch(
            "bot_bottle.backend.docker.util.runsc_available", return_value=False))
        # No orchestrator is probed over the network.
        self.report = self.enterContext(patch.object(
            doctor, "orchestrator_report",
            return_value=doctor.OrchestratorReport(_FC, True, ())))
        # Nor the host controller.
        self.enterContext(patch.object(doctor.runner, "running", return_value=None))

    def test_passes_when_python_and_backend_ready(self):
        with patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("ok:   python", out)
        self.assertIn("ok:   backend: docker: ready", out)

    def test_fails_when_no_backend_ready(self):
        # The regression the reviewer flagged: a backend whose binary is on PATH
        # but whose daemon/pool isn't ready must NOT pass. is_backend_ready is
        # the full status() check, so returning False here means "not ready".
        with patch.object(doctor, "known_backend_names", return_value=("docker", "firecracker")), \
                patch.object(doctor, "is_backend_ready", return_value=False):
            code, out = _run()
        self.assertEqual(1, code)
        self.assertIn("fail: backend", out)
        self.assertIn("warn: backend: docker: not ready", out)

    def test_passes_when_at_least_one_backend_ready(self):
        # docker not ready, firecracker ready → overall pass, mixed report.
        def ready(_settings: object, name: str, *, quiet: bool = False) -> bool:
            del quiet
            return name == "firecracker"

        with patch.object(doctor, "known_backend_names", return_value=("docker", "firecracker")), \
                patch.object(doctor, "is_backend_ready", side_effect=ready):
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("warn: backend: docker: not ready", out)
        self.assertIn("ok:   backend: firecracker: ready", out)

    def test_fails_when_python_too_old(self):
        # Force the version gate to fail without touching the interpreter.
        with patch.object(doctor, "MIN_PYTHON", (99, 0)), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            code, out = _run()
        self.assertEqual(1, code)
        self.assertIn("fail: python", out)

    def test_missing_config_dir_is_advisory_not_fatal(self):
        # A missing ~/.bot-bottle warns but must not fail. Point home at a
        # fresh empty dir so the shared suite HOME (which other tests may
        # populate) can't turn this into an "ok: config".
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(doctor.Path, "home", return_value=Path(tmp)), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("warn: config", out)

    def test_present_config_dir_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(doctor.Path, "home", return_value=Path(tmp)), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            (Path(tmp) / ".bot-bottle").mkdir()
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("ok:   config", out)


    def test_a_backend_for_another_os_is_skipped_in_a_line(self):
        # #684: doctor on macOS listed every Firecracker diagnostic.
        def os_req(name: str) -> str:
            return "Linux" if name == "firecracker" else ""
        self.os_req.side_effect = os_req
        with patch.object(doctor, "known_backend_names",
                          return_value=("firecracker", "macos-container")), \
                patch.object(doctor, "is_backend_ready", return_value=True) as ready:
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("note: backend: firecracker: skipped (Linux only)", out)
        self.assertEqual(["macos-container"], [c.args[1] for c in ready.call_args_list])

    def test_docker_as_the_only_ready_backend_gets_the_weak_boundary_note(self):
        with patch.object(doctor, "known_backend_names", return_value=("docker", "firecracker")), \
                patch.object(doctor, "is_backend_ready", side_effect=_only_docker_ready):
            _, out = _run()
        self.assertIn("note: backend: docker backend: agents are containers sharing "
                      "the host kernel", out)

    def test_docker_with_a_vm_backend_ready_gets_no_note(self):
        with patch.object(doctor, "known_backend_names", return_value=("docker", "firecracker")), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            _, out = _run()
        self.assertNotIn("host kernel", out)

    def test_the_note_says_when_gvisor_is_registered(self):
        with patch("bot_bottle.backend.docker.util.runsc_available", return_value=True), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_ready", return_value=True):
            _, out = _run()
        self.assertIn("gVisor (runsc) is registered", out)


def _delos_ready(_settings: object, name: str, *, quiet: bool = False) -> bool:
    """macos-container ready; docker installed with its daemon down, printing
    what its status() prints."""
    del quiet
    if name == "docker":
        print("docker on PATH: yes")
        print("docker daemon: UNREACHABLE")
        print("Run: bot-bottle install --backend=docker")
        return False
    return True


class TestDoctorSelectedBackend(unittest.TestCase):
    """doctor checks the backend a launch here would use in full; the others
    get one line and are never offered for install (2026-11-05, on delos)."""

    def setUp(self) -> None:
        self.enterContext(patch.object(doctor, "backend_os_requirement", return_value=""))
        self.enterContext(patch.object(doctor, "known_backend_names",
                                       return_value=("docker", "macos-container")))
        self.enterContext(patch.object(
            doctor, "orchestrator_report",
            return_value=doctor.OrchestratorReport(_FC, True, ())))
        self.running = self.enterContext(patch.object(doctor.runner, "running",
                                                      return_value=None))
        self.stale = self.enterContext(patch.object(doctor.runner, "restart_needed",
                                                    return_value=False))
        self.agents = self.enterContext(patch.object(doctor, "_has_agents", return_value=True))
        self.ready = self.enterContext(patch.object(doctor, "is_backend_ready",
                                                    side_effect=_delos_ready))

    def test_a_backend_not_in_use_is_one_line_and_never_offered(self):
        code, out = _run(selected="macos-container")
        self.assertEqual(0, code)
        self.assertIn("ok:   backend: macos-container: ready", out)
        self.assertIn("note: backend: docker: not ready (not used here; "
                      "BOT_BOTTLE_BACKEND=docker to use it)", out)
        self.assertNotIn("UNREACHABLE", out)
        self.assertNotIn("install --backend=docker", out)

    def test_verbose_shows_every_backends_diagnostics_indented(self):
        _, out = _run(["--verbose"], selected="macos-container")
        self.assertIn("      docker daemon: UNREACHABLE", out)

    def test_the_selected_backend_not_ready_fails_with_its_diagnostics(self):
        code, out = _run(selected="docker")
        self.assertEqual(1, code)
        self.assertIn("warn: backend: docker: not ready", out)
        self.assertIn("      docker daemon: UNREACHABLE", out)
        self.assertIn("fail: backend: docker isn't ready", out)
        self.assertIn("next: `bot-bottle install --backend=docker`", out)

    def test_next_prefers_a_restart_then_the_backend_then_setup_then_up(self):
        state = HostControlState(pid=7, port=1234, build_id="abc")
        self.running.return_value = state
        self.stale.return_value = True
        self.assertIn("next: `bot-bottle up --restart`", _run(selected="docker")[1])
        self.stale.return_value = False
        self.assertIn("next: `bot-bottle install --backend=docker`",
                      _run(selected="docker")[1])
        self.agents.return_value = False
        self.assertIn("next: `bot-bottle setup`", _run(selected="macos-container")[1])
        self.agents.return_value = True
        self.running.return_value = None
        self.assertIn("next: `bot-bottle up`, then run `bot-bottle`",
                      _run(selected="macos-container")[1])
        self.running.return_value = state
        self.assertIn("next: run `bot-bottle`", _run(selected="macos-container")[1])

    def test_next_alone_prints_only_the_step(self):
        self.running.return_value = HostControlState(pid=7, port=1234, build_id="abc")
        self.stale.return_value = True
        code, out = _run(["--next"], selected="macos-container")
        self.assertEqual(0, code)
        self.assertEqual("`bot-bottle up --restart` (the host controller runs older code; "
                         "running bottles keep running)\n", out)

    def test_with_no_backend_at_all_next_says_to_set_one_up(self):
        self.ready.side_effect = None
        self.ready.return_value = False
        code, out = _run(selected="")
        self.assertEqual(1, code)
        self.assertIn("next: set up a backend", out)


class TestHasAgents(unittest.TestCase):
    def test_the_architect_alone_is_no_agent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(doctor, "bot_bottle_root", return_value=Path(tmp)):
            self.assertFalse(doctor._has_agents())  # pylint: disable=protected-access
            agents = Path(tmp) / "agents"
            agents.mkdir()
            (agents / "bottle-architect.md").write_text("x")
            self.assertFalse(doctor._has_agents())  # pylint: disable=protected-access
            (agents / "claude.md").write_text("x")
            self.assertTrue(doctor._has_agents())  # pylint: disable=protected-access


class TestDoctorOrchestrator(unittest.TestCase):
    """doctor reports the environment's orchestrator and warns about other
    bot-bottle orchestrators listening; advisory, so the exit code is untouched."""

    def _orchestrator_lines(self, report: "doctor.OrchestratorReport") -> str:
        with patch.object(doctor, "orchestrator_report", return_value=report), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "backend_os_requirement", return_value=""), \
                patch.object(doctor, "is_backend_ready", return_value=True), \
                patch.object(doctor.runner, "running", return_value=None), \
                patch("bot_bottle.backend.docker.util.runsc_available", return_value=False):
            code, out = _run()
        self.assertEqual(0, code)
        return "\n".join(line for line in out.splitlines() if " orchestrator: " in line)

    def test_the_environments_orchestrator_is_ok(self):
        out = self._orchestrator_lines(doctor.OrchestratorReport(_FC, True, ()))
        self.assertIn(f"ok:   orchestrator: firecracker at {_FC.url}", out)
        self.assertNotIn("warn", out)

    def test_a_stray_orchestrator_is_warned_about_with_how_to_remove_it(self):
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.docker.infra import GATEWAY_NAME, ORCHESTRATOR_NAME
        out = self._orchestrator_lines(doctor.OrchestratorReport(_FC, True, (_STRAY,)))
        self.assertIn(f"warn: orchestrator: another bot-bottle orchestrator (docker) "
                      f"answers at {_STRAY.url}", out)
        self.assertIn(f"docker rm -f {ORCHESTRATOR_NAME} {GATEWAY_NAME}", out)

    def test_none_recorded_or_down_is_a_warning(self):
        out = self._orchestrator_lines(doctor.OrchestratorReport(None, False, (_STRAY,)))
        self.assertIn("warn: orchestrator: no orchestrator for environment", out)
        self.assertIn("`bot-bottle up --backend docker` adopts it", out)
        out = self._orchestrator_lines(doctor.OrchestratorReport(_FC, False, ()))
        self.assertIn(f"warn: orchestrator: firecracker at {_FC.url} isn't answering", out)

    def test_the_report_compares_the_listening_ones_with_the_record(self):
        with patch.object(doctor, "recorded_orchestrator", return_value=_FC), \
                patch.object(doctor.OrchestratorClient, "health", return_value=True), \
                patch("bot_bottle.control.client.listening_orchestrators",
                      return_value=[_STRAY, _FC]), \
                patch("bot_bottle.control.client.recorded_orchestrator", return_value=_FC):
            report = doctor.orchestrator_report()
        self.assertEqual(doctor.OrchestratorReport(_FC, True, (_STRAY,)), report)


class TestDoctorHostController(unittest.TestCase):
    """doctor says when the running host controller predates an upgrade (it
    refuses launches until restarted); advisory, so the exit code is untouched."""

    _STATE = HostControlState(pid=7, port=1234, build_id="abc")

    def _host_controller_line(self, state: "HostControlState | None", stale: bool) -> str:
        with patch.object(doctor, "orchestrator_report",
                          return_value=doctor.OrchestratorReport(_FC, True, ())), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "backend_os_requirement", return_value=""), \
                patch.object(doctor, "is_backend_ready", return_value=True), \
                patch.object(doctor.runner, "running", return_value=state), \
                patch.object(doctor.runner, "restart_needed", return_value=stale), \
                patch("bot_bottle.backend.docker.util.runsc_available", return_value=False):
            code, out = _run()
        self.assertEqual(0, code)
        return "\n".join(line for line in out.splitlines() if "host controller" in line)

    def test_a_current_host_controller_is_ok(self):
        self.assertEqual("ok:   host controller: running at http://127.0.0.1:1234 (build abc)",
                         self._host_controller_line(self._STATE, stale=False))

    def test_one_started_before_an_upgrade_is_a_warning_with_the_fix(self):
        line = self._host_controller_line(self._STATE, stale=True)
        self.assertIn("warn: host controller: restart needed (installed version changed)", line)
        self.assertIn("`bot-bottle up --restart`", line)

    def test_none_running_is_a_note(self):
        self.assertEqual("note: host controller: not running (`bot-bottle up` starts it)",
                         self._host_controller_line(None, stale=False))


if __name__ == "__main__":
    unittest.main()


class NextBlock(unittest.TestCase):
    def test_the_command_stands_alone_with_the_rest_under_it(self) -> None:
        from bot_bottle.cli.commands.doctor import next_block
        block = next_block("`bot-bottle install --backend=firecracker` (it prints any sudo "
                           "steps it needs)", color=False)
        self.assertIn("\n    bot-bottle install --backend=firecracker\n", block)
        self.assertIn("  (it prints any sudo steps it needs)", block)
        self.assertIn("then run `bot-bottle`",
                      next_block("`bot-bottle up`, then run `bot-bottle`", color=False))
        self.assertEqual("\nNext: set up a backend\n", next_block("set up a backend", color=False))
        self.assertIn("\x1b[1;36mbot-bottle\x1b[0m", next_block("run `bot-bottle`", color=True))

    def test_a_terminal_gets_the_block_and_a_pipe_the_line(self) -> None:
        from bot_bottle.cli.commands import doctor
        with patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(out, "isatty", return_value=True, create=True), \
                patch.dict(os.environ, {"NO_COLOR": "1"}):
            doctor._print_next("run `bot-bottle`")  # pylint: disable=protected-access
        self.assertIn("Next, run:", out.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            doctor._print_next("run `bot-bottle`")  # pylint: disable=protected-access
        self.assertEqual("next: run `bot-bottle`\n", out.getvalue())
