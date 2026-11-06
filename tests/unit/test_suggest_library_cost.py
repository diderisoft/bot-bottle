"""The architect sees what stacking a bottle costs: every repo it grants and
its size, and is told a repo grant needs no bottle stacked for its forge."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest import architect, starter
from tests.unit._forges import connected
from tests.unit.test_suggest_runnable import CONN

_BIG = """---
git-gate:
  repos:
    - gitea.dideric.is/didericis/app:write
    - gitea.dideric.is/didericis/site:write
egress:
  routes:
    - host: registry.npmjs.org
---

App work, with its repos.
"""


class TestLibraryCost(unittest.TestCase):
    def test_entries_carry_their_repos_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bottles = root / "bottles"
            bottles.mkdir()
            (bottles / "small.md").write_text("---\nenv:\n  A: b\n---\n\nSmall.\n")
            (bottles / "app-dev.md").write_text(_BIG)
            with connected(CONN):
                library = {e["name"]: e for e in architect._host_library(root, [(bottles, False)])}  # pylint: disable=protected-access
        self.assertEqual([], library["small"]["repos"])
        self.assertEqual(2, len(library["app-dev"]["repos"]))
        self.assertTrue(all("didericis/" in r for r in library["app-dev"]["repos"]))
        self.assertGreater(library["app-dev"]["element_count"], library["small"]["element_count"])
        self.assertNotIn("forges", library["app-dev"])

    def test_a_bottle_whose_elements_cant_be_read_still_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bottles = root / "bottles"
            bottles.mkdir()
            (bottles / "app-dev.md").write_text(_BIG)
            with mock.patch("bot_bottle.control.hostctl.manifest_elements.bottle_elements",
                            side_effect=RuntimeError("unreadable")):
                library = {e["name"]: e for e in architect._host_library(root, [(bottles, False)])}  # pylint: disable=protected-access
        self.assertEqual(([], 0, []), (library["app-dev"]["elements"],
                                       library["app-dev"]["element_count"],
                                       library["app-dev"]["repos"]))

    def test_the_starter_says_not_to_stack_for_a_forge(self) -> None:
        self.assertIn("needs no bottle stacked for its forge", starter.ARCHITECT_STARTER)
        self.assertIn("never stack a bottle\n  just to reach a forge", starter.ARCHITECT_STARTER)

    def test_the_prompt_lists_the_hosts_connections(self) -> None:
        with connected(CONN):
            forges = architect._connected_forges()  # pylint: disable=protected-access
        self.assertEqual([{"name": "gitea.dideric.is", "kind": "gitea",
                           "url": "https://gitea.dideric.is"}], forges)


if __name__ == "__main__":
    unittest.main()
