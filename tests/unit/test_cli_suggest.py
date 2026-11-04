"""Tests for `bot-bottle bottles suggest` CLI (PRD 0117)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands import bottles


class TestBottlesSuggestCli(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        cwd = mock.patch("bot_bottle.cli.commands.bottles.os.getcwd",
                         return_value=str(self.cwd))
        cwd.start()
        self.addCleanup(cwd.stop)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = bottles.cmd_bottles(["suggest"] + argv)
        return code, out.getvalue()

    def test_suggest_prints_option(self) -> None:
        code, out = self._run(["deploy this app"])
        self.assertEqual(code, 0)
        self.assertIn("option:", out)

    def test_suggest_json_output(self) -> None:
        code, out = self._run(["do something", "--json"])
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertIn("option", d)
        self.assertIn("bottles", d)

    def test_suggest_no_task_fails(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            try:
                code = bottles.cmd_bottles(["suggest"])
            except SystemExit as e:
                code = e.code  # argparse exits on missing required arg
        self.assertNotEqual(code, 0)

    def test_suggest_write_saves_bottle(self) -> None:
        code, _ = self._run(["do something", "--write", "my-bottle"])
        self.assertEqual(code, 0)
        bottle_path = self.root / "bottles" / "my-bottle.md"
        self.assertTrue(bottle_path.exists(), f"{bottle_path} was not written")

    def test_suggest_write_invalid_name_fails(self) -> None:
        code, _ = self._run(["task", "--write", "Bad Name"])
        self.assertNotEqual(code, 0)

    def test_suggest_write_existing_name_fails(self) -> None:
        (self.root / "bottles" / "existing.md").write_text("---\n---\n\nExisting.\n")
        code, _ = self._run(["task", "--write", "existing"])
        self.assertNotEqual(code, 0)

    def test_suggest_cwd_flag(self) -> None:
        other = self.home / "other"
        other.mkdir()
        code, _ = self._run(["do something", "--cwd", str(other)])
        self.assertEqual(code, 0)

    def test_suggest_rules_notes_included(self) -> None:
        code, out = self._run(["do something"])
        self.assertEqual(code, 0)
        # The rules path always adds a note.
        self.assertIn("note:", out)

    def test_the_default_is_the_architect(self) -> None:
        from bot_bottle.suggest import flow  # pylint: disable=import-outside-toplevel
        with mock.patch.object(flow, "architect_suggest",
                               side_effect=flow.ArchitectError("no agent")) as arch:
            code, out = self._run(["do something", "--json"])
        self.assertEqual(code, 0)
        arch.assert_called_once()
        d = json.loads(out)
        self.assertEqual("rules", d["source"])
        self.assertTrue(any("bottle-architect didn't answer (no agent)" in n for n in d["notes"]))

    def test_rules_flag_skips_the_architect(self) -> None:
        from bot_bottle.suggest import flow  # pylint: disable=import-outside-toplevel
        with mock.patch.object(flow, "architect_suggest") as arch:
            code, out = self._run(["do something", "--rules"])
        self.assertEqual(code, 0)
        arch.assert_not_called()
        self.assertIn("from:    rules", out)

    def test_the_old_architect_flag_is_gone(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            bottles.cmd_bottles(["suggest", "do something", "--architect"])
        self.assertEqual(2, cm.exception.code)


if __name__ == "__main__":
    unittest.main()
