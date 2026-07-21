"""The CLI package is runnable as `python -m bot_bottle.cli`."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bot_bottle.cli", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestModuleEntry(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        result = _run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("login", result.stderr)

    def test_no_args_prints_usage(self) -> None:
        # main() returns 2 with no command, matching the cli.py entry point.
        self.assertEqual(_run().returncode, 2)

    def test_subcommand_help_reaches_handler(self) -> None:
        result = _run("login", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--console-url", result.stderr)


if __name__ == "__main__":
    unittest.main()
