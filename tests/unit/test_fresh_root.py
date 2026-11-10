"""Unit: a fresh install (#580's live test found both).

- A CLI command neither creates nor migrates the host DB, and never asks to:
  the host controller does that when it starts.
- `$BOT_BOTTLE_ROOT` moves the manifests (agents, bottles) with the data.
- A named environment run from `~` doesn't list `~/.bot-bottle` (live's
  root) as a project's manifests.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from bot_bottle.cli import __main__ as cli_main
from bot_bottle.manifest import ManifestIndex
from bot_bottle.paths import bot_bottle_root, host_db_path


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "root"
        env = mock.patch.dict(os.environ, {"HOME": str(self.base / "home"),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def run_list(self, answer: str = "") -> tuple[int, str]:
        def handler(_rest: list[str]) -> int:
            return 0
        err = io.StringIO()
        with mock.patch.dict(cli_main.COMMANDS, {"list": handler}), \
                mock.patch.object(cli_main.sys, "stdin", io.StringIO(answer)), \
                redirect_stderr(err):
            code = cli_main.main(["list"])
        return code, err.getvalue()


class TheCliLeavesTheHostDbAlone(_Root):
    """The host controller creates and migrates the host DB when it starts
    (ADR 0020); a CLI command never does, and never asks."""

    def test_a_command_in_a_new_root_creates_no_db(self) -> None:
        code, err = self.run_list()
        self.assertEqual(0, code)
        self.assertNotIn("out of date", err)
        self.assertFalse(host_db_path().exists())

    def test_an_old_db_is_not_questioned(self) -> None:
        host_db_path().parent.mkdir(parents=True)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE something_older (x INTEGER)")
        self.assertEqual((0, ""), self.run_list())


class ManifestRoot(_Root):
    def test_agents_and_bottles_load_from_bot_bottle_root(self) -> None:
        self.assertEqual(self.root, bot_bottle_root())
        (self.root / "agents").mkdir(parents=True)
        (self.root / "bottles").mkdir()
        (self.root / "agents" / "demo.md").write_text("---\n---\n\nHi.\n")
        (self.root / "bottles" / "dev.md").write_text("---\n---\n")
        (self.base / "home").mkdir()
        index = ManifestIndex.resolve(str(self.base / "elsewhere"))
        self.assertEqual(["demo"], index.all_agent_names)
        self.assertEqual("Hi.", index.load_for_agent("demo", ("dev",)).agent.prompt)

    def test_without_the_override_it_is_home(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": ""}):
            self.assertEqual(Path("/h/.bot-bottle"), bot_bottle_root(Path("/h")))


if __name__ == "__main__":
    unittest.main()


class NamedEnvironmentAtHome(unittest.TestCase):
    """From the home directory, a named environment's index must not take the
    live root (`~/.bot-bottle`) for a project's manifests."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "BOT_BOTTLE_ENV": "dev"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BOT_BOTTLE_ROOT", None)
        for root, agent in ((self.home / ".bot-bottle", "live-agent"),
                            (bot_bottle_root(), "dev-agent")):
            (root / "agents").mkdir(parents=True)
            (root / "bottles").mkdir()
            (root / "agents" / f"{agent}.md").write_text("---\n---\n\nHi.\n")
            (root / "bottles" / "b.md").write_text("---\n---\n")

    def test_the_live_root_is_not_a_project(self) -> None:
        index = ManifestIndex.resolve(str(self.home))
        self.assertIsNone(index.cwd_md)
        self.assertEqual(["dev-agent"], index.all_agent_names)

    def test_a_real_project_still_counts(self) -> None:
        project = self.home / "src" / "app"
        (project / ".bot-bottle" / "agents").mkdir(parents=True)
        (project / ".bot-bottle" / "agents" / "app-agent.md").write_text(
            "---\n---\n\nHi.\n")
        index = ManifestIndex.resolve(str(project))
        self.assertEqual(["app-agent", "dev-agent"], index.all_agent_names)
