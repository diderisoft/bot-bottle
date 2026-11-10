"""`bot-bottle bottles`: the bottle definitions this directory sees, and their
versions through `bottles history|revert`."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli.commands import bottles


class TestBottles(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BOT_BOTTLE_ROOT", None)
        root = self.home / ".bot-bottle" / "bottles"
        root.mkdir(parents=True)
        (root / "work.md").write_text("---\nextends: basic\nsupervise: true\n---\nWork.\n")
        (root / "broken.md").write_text(
            "---\negress:\n  routes:\n    - host: x\n      bogus: {}\n---\n")
        cwd = patch("bot_bottle.cli.commands.bottles.os.getcwd", return_value=str(self.home))
        cwd.start()
        self.addCleanup(cwd.stop)

    def run_cmd(self, argv: list[str]) -> tuple[int, dict[str, list[str]]]:
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = bottles.cmd_bottles(argv)
        return code, {line.split("\t")[0]: line.split("\t") for line in out.getvalue().splitlines()}

    def test_lists_host_and_built_in_bottles(self) -> None:
        code, rows = self.run_cmd([])
        self.assertEqual(0, code)
        self.assertEqual(["work", "host", "basic", "-", "-"], rows["work"])
        self.assertEqual("built-in", rows["basic"][1])
        self.assertIn("unknown key 'bogus'", rows["broken"][4])

    def test_history_and_revert_are_its_subcommands(self) -> None:
        with patch("bot_bottle.cli.commands.bottle.cmd_bottle", return_value=0) as old:
            self.assertEqual(0, bottles.cmd_bottles(["history", "work"]))
        old.assert_called_once_with(["history", "work"])

    def test_an_unknown_argument(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, bottles.cmd_bottles(["bogus"]))


if __name__ == "__main__":
    unittest.main()
