"""The architect sees what stacking a bottle costs: every repo it grants and
its size, so it can pick the smallest bottle that declares a forge."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest import architect, starter
from tests.unit.test_suggest_runnable import _FORGE

_BIG = """---
extends: gitea
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
            (bottles / "gitea.md").write_text(f"---\n{_FORGE}---\n\nThe gitea forge.\n")
            (bottles / "app-dev.md").write_text(_BIG)
            library = {e["name"]: e for e in architect._host_library(root, [(bottles, False)])}  # pylint: disable=protected-access
        self.assertEqual([], library["gitea"]["repos"])
        self.assertEqual(2, len(library["app-dev"]["repos"]))
        self.assertTrue(all("didericis/" in r for r in library["app-dev"]["repos"]))
        self.assertGreater(library["app-dev"]["element_count"], library["gitea"]["element_count"])
        self.assertEqual(["gitea.dideric.is"], library["app-dev"]["forges"])

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

    def test_the_starter_says_to_use_the_smallest_forge_bottle(self) -> None:
        self.assertIn("smallest bottle that\n  declares it", starter.ARCHITECT_STARTER)
        self.assertIn("never a bigger bottle that extends it", starter.ARCHITECT_STARTER)


if __name__ == "__main__":
    unittest.main()
