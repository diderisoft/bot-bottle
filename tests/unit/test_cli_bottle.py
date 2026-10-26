"""`bot-bottle bottle history | revert` (PRD 0103)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from bot_bottle.cli.commands.bottle import cmd_bottle
from bot_bottle.lib.log import Die
from bot_bottle.manifest.versions import history, record
from tests.unit import use_bottle_root


class TestBottleCommand(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / "root"
        (self.root / "bottles").mkdir(parents=True)
        (self.root / "agents").mkdir()
        self.addCleanup(use_bottle_root(self.root))
        cwd = os.getcwd()
        os.chdir(tmp)
        self.addCleanup(os.chdir, cwd)
        self.path = self.root / "bottles" / "dev.md"
        self.path.write_text("---\nextends: basic\n---\n")
        self.first = record("first", root=self.root)
        self.path.write_text("---\nextends: basic\nenv:\n  A: b\n---\n")

    def run_cmd(self, *argv: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cmd_bottle(list(argv)))
        return out.getvalue()

    def test_history_versions_hand_edits_and_shows_one(self) -> None:
        out = self.run_cmd("history", "dev")
        self.assertIn("edited by hand", out)
        self.assertIn(self.first[:9], out)
        self.assertIn("+  A: b", self.run_cmd("history", "dev", history(self.path)[0].sha[:9]))

    def test_revert(self) -> None:
        self.run_cmd("revert", "dev", self.first[:9])
        self.assertEqual("---\nextends: basic\n---\n", self.path.read_text())

    def test_errors(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, cmd_bottle([]))
            for argv in (["history", "nope"], ["history", "basic"], ["revert", "dev"],
                         ["revert", "dev", "deadbeef"]):
                with self.assertRaises(Die):
                    cmd_bottle(argv)


if __name__ == "__main__":
    unittest.main()
