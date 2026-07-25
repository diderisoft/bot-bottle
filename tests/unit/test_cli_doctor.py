"""Unit: `bot-bottle doctor` host prerequisite checks (ADR 0004).

`doctor` is a store-free diagnostic — it must run on a fresh install
before any DB migration, and its exit code gates only the two hard
prerequisites (Python and an available backend). The config-dir check is
advisory and never affects the exit code.
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
    def test_passes_when_python_and_backend_ok(self):
        with patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_available", return_value=True):
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("ok:   python", out)
        self.assertIn("ok:   backend: available: docker", out)

    def test_fails_when_no_backend_available(self):
        with patch.object(doctor, "known_backend_names", return_value=("docker", "firecracker")), \
                patch.object(doctor, "is_backend_available", return_value=False):
            code, out = _run()
        self.assertEqual(1, code)
        self.assertIn("fail: backend", out)

    def test_fails_when_python_too_old(self):
        # Force the version gate to fail without touching the interpreter.
        with patch.object(doctor, "MIN_PYTHON", (99, 0)), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_available", return_value=True):
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
                patch.object(doctor, "is_backend_available", return_value=True):
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("warn: config", out)

    def test_present_config_dir_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(doctor.Path, "home", return_value=Path(tmp)), \
                patch.object(doctor, "known_backend_names", return_value=("docker",)), \
                patch.object(doctor, "is_backend_available", return_value=True):
            (Path(tmp) / ".bot-bottle").mkdir()
            code, out = _run()
        self.assertEqual(0, code)
        self.assertIn("ok:   config", out)


if __name__ == "__main__":
    unittest.main()
