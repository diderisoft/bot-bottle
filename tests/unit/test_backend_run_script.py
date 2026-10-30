"""Unit: `run_script`, behind every backend's `Bottle.exec`."""

from __future__ import annotations

import time
import unittest

from bot_bottle.backend.base import TIMED_OUT, run_script


class TestRunScript(unittest.TestCase):
    def test_runs_the_script_from_stdin(self) -> None:
        result = run_script(["sh", "-s"], "echo out; echo err >&2; exit 3")
        self.assertEqual((3, "out\n", "err\n"), (result.returncode, result.stdout, result.stderr))

    def test_a_command_past_its_timeout_is_killed(self) -> None:
        # A wedged `container exec` held its caller for hours.
        started = time.monotonic()
        result = run_script(["sh", "-s"], "sleep 30", timeout=0.3)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(TIMED_OUT, result.returncode)
        self.assertIn("timed out after 0.3s", result.stderr)


if __name__ == "__main__":
    unittest.main()
