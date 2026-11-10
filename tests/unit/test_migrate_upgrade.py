"""`bottles migrate` / `agents migrate`: each old form's conversion to the
current one (`manifest/upgrade.py`), written into the file keeping what it
can, and a second run finding nothing to do."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from bot_bottle.manifest.migrate import MigrateError, apply, prepare, prepare_agent
from bot_bottle.manifest.upgrade import upgrade_agent, upgrade_bottle


def bottle(fm: dict[str, Any]) -> dict[str, Any]:
    return upgrade_bottle(fm)[0]


def agent(fm: dict[str, Any]) -> dict[str, Any]:
    return upgrade_agent(fm)[0]


class _Host(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp) / ".bot-bottle"
        self.bottles = self.root / "bottles"
        self.agents = self.root / "agents"
        self.bottles.mkdir(parents=True)
        self.agents.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)}))
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def migrate(self, text: str, name: str = "dev") -> str:
        path = self.bottles / f"{name}.md"
        path.write_text(text)
        apply(prepare(name, self.dirs), self.dirs, root=self.root)
        with self.assertRaisesRegex(MigrateError, "nothing to migrate"):
            prepare(name, self.dirs)
        load_bottle_chain(name, self.dirs)
        return path.read_text()

    def migrate_agent(self, text: str, name: str = "dev") -> str:
        path = self.agents / f"{name}.md"
        path.write_text(text)
        apply(prepare_agent(name, [(self.agents, False)]), self.dirs, root=self.root)
        with self.assertRaisesRegex(MigrateError, "nothing to migrate"):
            prepare_agent(name, [(self.agents, False)])
        return path.read_text()


class TestEnv(_Host):
    def test_a_bare_question_becomes_a_secret_one_on_its_line(self) -> None:
        self.assertEqual({"env": {"A": "secret:?", "B": "secret:?Paste B", "C": "${C}",
                                  "D": "lit"}},
                         bottle({"env": {"A": "?", "B": "?Paste B", "C": "${C}", "D": "lit"}}))
        after = self.migrate("---\n# mine\nenv:\n  A: ?   # asked\n  B: \"?Paste B\"\n"
                             "  C: ${C}\n---\nBody.\n")
        self.assertEqual("---\n# mine\nenv:\n  A: secret:?   # asked\n"
                         "  B: \"secret:?Paste B\"\n  C: ${C}\n---\nBody.\n", after)


if __name__ == "__main__":
    unittest.main()
