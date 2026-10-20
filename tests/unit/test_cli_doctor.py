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


def _run(argv: list[str] | None = None) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = doctor.cmd_doctor(argv or [])
    return code, buf.getvalue()


class TestDoctor(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
