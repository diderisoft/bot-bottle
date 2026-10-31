"""Tests for the rules-based bottle suggestion path (PRD 0117)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.suggest.proposal import (
    OPTION_REUSE,
    OPTION_STACK_ADDITIONS,
)
from bot_bottle.suggest.rules import rules_suggest


_GITEA_BOTTLE = """\
---
credentials:
  gitea-api:
    source: secret
    secret: GITEA_TOKEN
git-gate:
  forges:
    gitea.example.com:
      kind: gitea
      url: https://gitea.example.com
      ssh: ssh://git@gitea.example.com:2222
      token: $gitea-api
      host_key: ssh-ed25519 AAAA
---

The gitea forge bottle.
"""

_MAIN_BOTTLE = """\
---
extends: basic
---

The project main bottle.
"""

# A project bottle that grants something beyond basic — requires trust.
_MAIN_BOTTLE_WITH_EGRESS = """\
---
extends: basic
egress:
  routes:
    - host: api.internal.example.com
---

The project main bottle with extra egress.
"""


class TestRulesNoProject(unittest.TestCase):
    def test_falls_back_to_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / ".bot-bottle"
            (root / "bottles").mkdir(parents=True)
            cwd = home / "myproject"
            cwd.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home),
                                              "BOT_BOTTLE_ROOT": str(root)}):
                p = rules_suggest("do something", cwd, host_root=root)
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertEqual(p.bottles, ["basic"])
        self.assertTrue(p.additions.is_empty())


class TestRulesWithForge(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        self.bottles = self.root / "bottles"
        self.bottles.mkdir(parents=True)
        (self.bottles / "gitea.md").write_text(_GITEA_BOTTLE)
        self.cwd = self.home / "myproject"
        self.cwd.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def _set_origin(self, url: str) -> None:
        import subprocess
        subprocess.run(["git", "init", str(self.cwd)], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.cwd), "remote", "add", "origin", url],
                       check=True, capture_output=True)

    def test_connected_forge_adds_grant(self) -> None:
        self._set_origin("https://gitea.example.com/acme/myapp.git")
        p = rules_suggest("deploy the app", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_STACK_ADDITIONS)
        self.assertIn("gitea", p.bottles)
        self.assertEqual(len(p.additions.repos), 1)
        self.assertIn("acme/myapp", p.additions.repos[0].grant)
        self.assertIn(":write", p.additions.repos[0].grant)

    def test_unconnected_host_falls_back_to_basic(self) -> None:
        self._set_origin("https://github.com/acme/myapp.git")
        p = rules_suggest("deploy the app", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertEqual(p.bottles, ["basic"])


class TestRulesWithTrustedProjectBottle(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        self.cwd = self.home / "myproject"
        self.project_bottles = self.cwd / ".bot-bottle" / "bottles"
        self.project_bottles.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def _trust_main(self) -> None:
        from bot_bottle.approvals import trust_project_bottle
        trust_project_bottle(self.cwd, "main")

    def test_trusted_project_bottle_is_reused(self) -> None:
        (self.project_bottles / "main.md").write_text(_MAIN_BOTTLE)
        self._trust_main()
        p = rules_suggest("work on this repo", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertIn("./main", p.bottles)

    def test_untrusted_bottle_with_grants_falls_through(self) -> None:
        # A bottle that grants something beyond basic requires trust.
        (self.project_bottles / "main.md").write_text(_MAIN_BOTTLE_WITH_EGRESS)
        # Don't call _trust_main() — bottle exists but is untrusted.
        p = rules_suggest("work on this repo", self.cwd, host_root=self.root)
        # With no trusted project bottle and no forge, falls to basic.
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertEqual(p.bottles, ["basic"])

    def test_project_bottle_extends_basic_only_is_accepted(self) -> None:
        # A bottle with no grants beyond basic passes trust check automatically.
        (self.project_bottles / "main.md").write_text(_MAIN_BOTTLE)
        p = rules_suggest("work on this repo", self.cwd, host_root=self.root)
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertIn("./main", p.bottles)


if __name__ == "__main__":
    unittest.main()
