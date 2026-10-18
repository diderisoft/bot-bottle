"""PRD 0085: a published ext4 is smoke-tested in the first bottle booted from it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from bot_bottle.backend.firecracker import launch as mod
from bot_bottle.backend.base import ExecResult


class SmokeTestPublishedBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ext4 = self.dir / "agent-ext4-x.ext4"
        self.ext4.write_bytes(b"")

    def _bottle(self, rc: int = 0) -> Any:
        bottle = MagicMock()
        bottle.exec.return_value = ExecResult(returncode=rc, stdout="", stderr="not found")
        return bottle

    def _run(self, bottle: Any, base: Path, smoke: tuple[str, ...] = ("claude", "--version")) -> None:
        mod._smoke_test_published_base(bottle, base, smoke)  # pyright: ignore[reportPrivateUsage]

    def test_first_boot_runs_it_once_then_marks_the_image(self) -> None:
        bottle = self._bottle()
        self._run(bottle, self.ext4)
        bottle.exec.assert_called_once_with("claude --version")
        self.assertTrue(self.ext4.with_suffix(".smoked").is_file())
        self._run(bottle, self.ext4)
        bottle.exec.assert_called_once()  # not repeated

    def test_failure_dies_and_leaves_no_marker(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(self._bottle(rc=127), self.ext4)
        self.assertFalse(self.ext4.with_suffix(".smoked").exists())

    def test_built_rootfs_dirs_are_skipped(self) -> None:
        # Images built here were already smoke-tested before export.
        bottle = self._bottle()
        self._run(bottle, self.dir)
        bottle.exec.assert_not_called()

    def test_no_declared_smoke_test_is_a_noop(self) -> None:
        bottle = self._bottle()
        self._run(bottle, self.ext4, smoke=())
        bottle.exec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
