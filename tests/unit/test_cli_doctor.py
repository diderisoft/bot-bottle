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
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.commands import doctor
from bot_bottle.control.client import OrchestratorEndpoint


def _run(argv: list[str] | None = None) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
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


class TestDoctorOrchestrator(unittest.TestCase):
    """doctor reports the environment's orchestrator and warns about other
    bot-bottle orchestrators listening; advisory, so the exit code is untouched."""

    def _orchestrator_lines(self, report: "doctor.OrchestratorReport") -> str:
        with patch.object(doctor, "orchestrator_report", return_value=report), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "backend_os_requirement", return_value=""), \
                patch.object(doctor, "is_backend_ready", return_value=True), \
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


if __name__ == "__main__":
    unittest.main()
