"""Unit: a fresh install (#580's live test found both).

- A brand-new root's host DB is created on the first command, without the
  "database schema is out of date" question meant for upgrades.
- So is one the host controller made first (it starts before any command
  that migrates, in a new environment).
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
from bot_bottle.control.hostctl.__main__ import migrate_new_host_db
from bot_bottle.control.hostctl.audit_forward import ForwardCursors
from bot_bottle.manifest import ManifestIndex
from bot_bottle.orchestrator.store.store_manager import StoreManager
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


class FreshDb(_Root):
    def test_a_new_root_is_created_without_asking(self) -> None:
        code, err = self.run_list()
        self.assertEqual(0, code)
        self.assertNotIn("out of date", err)
        self.assertTrue(StoreManager(host_db_path()).is_migrated())

    def test_an_empty_db_file_counts_as_new(self) -> None:
        host_db_path().parent.mkdir(parents=True)
        sqlite3.connect(host_db_path()).close()
        self.assertEqual((0, ""), self.run_list())

    def test_an_existing_db_behind_the_schema_still_asks(self) -> None:
        host_db_path().parent.mkdir(parents=True)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE something_older (x INTEGER)")
        code, err = self.run_list(answer="")
        self.assertEqual(1, code)
        self.assertIn("out of date", err)
        code, err = self.run_list(answer="y\n")
        self.assertEqual(0, code)
        self.assertTrue(StoreManager(host_db_path()).is_migrated())


class HostControllerCreatesDb(_Root):
    """The host controller starts before any migrating command in a new
    environment (`up` skips the gate) and writes its own table first."""

    def test_its_db_is_migrated_so_the_cli_does_not_ask(self) -> None:
        migrate_new_host_db()
        ForwardCursors(host_db_path())
        self.assertTrue(StoreManager(host_db_path()).is_migrated())
        self.assertEqual((0, ""), self.run_list())

    def test_it_leaves_an_existing_db_to_the_cli(self) -> None:
        host_db_path().parent.mkdir(parents=True)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE something_older (x INTEGER)")
        migrate_new_host_db()
        self.assertFalse(StoreManager(host_db_path()).is_migrated())


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
