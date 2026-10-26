"""Bottle onboarding (PRD 0103)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import onboarding
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.onboarding import Draft, ForgeMatch, Origin, find_forge, onboard, parse_origin

FORGE_BOTTLE = """---
git-gate:
  forges:
    gitea.example.com:
      url: https://gitea.example.com
      ssh: ssh://git@10.0.0.1:30009
      token: $t
---
"""


class TestOrigin(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(Origin("10.0.0.1", "30009", "acme", "app"),
                         parse_origin("ssh://git@10.0.0.1:30009/acme/app.git"))
        self.assertEqual(Origin("gitea.example.com", "", "acme", "app"),
                         parse_origin("git@gitea.example.com:acme/app.git"))
        self.assertEqual(Origin("github.com", "", "acme", "app"),
                         parse_origin("https://github.com/acme/app"))
        self.assertIsNone(parse_origin("https://github.com/acme"))
        self.assertIsNone(parse_origin("/some/local/path"))


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.host = tmp / "bottles"
        self.host.mkdir()
        (self.host / "gitea.md").write_text(FORGE_BOTTLE)
        self.project = tmp / "app"
        self.project.mkdir()


class TestFindForge(_Dirs):
    def test_by_ssh_or_web_address(self) -> None:
        match = ForgeMatch("gitea", "gitea.example.com")
        self.assertEqual(match, find_forge(Origin("10.0.0.1", "30009", "a", "b"), self.host))
        self.assertEqual(match, find_forge(Origin("gitea.example.com", "", "a", "b"), self.host))
        self.assertIsNone(find_forge(Origin("10.0.0.1", "22", "a", "b"), self.host))
        self.assertIsNone(find_forge(Origin("github.com", "", "a", "b"), self.host))
        (self.host / "broken.md").write_text("---\n: x\n---\n")
        self.assertIsNone(find_forge(Origin("github.com", "", "a", "b"), self.host))
        self.assertIsNone(find_forge(Origin("x", "", "a", "b"), self.host / "nope"))


class TestOnboard(_Dirs):
    def run_onboard(self, origin: str | None, replies: list[str],
                    connect: bool = False) -> tuple[str | None, list[str]]:
        if origin:
            subprocess.run(["git", "init", "-q", str(self.project)], check=True)
            subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin", origin],
                           check=True)
        said: list[str] = []
        answers = iter(replies)
        with mock.patch("bot_bottle.approvals.trust_project_bottle") as trust:
            result = onboard(self.project, self.host, ask=lambda _q: next(answers),
                             say=said.append, connect=lambda _url: connect)
        if result:
            trust.assert_called_once_with(self.project, "main")
        return result, said

    def written(self) -> dict[str, object]:
        path = self.project / ".bot-bottle" / "bottles" / "main.md"
        fm, _ = parse_frontmatter(path.read_text())
        return fm

    def test_drafts_the_origin_read_only(self) -> None:
        result, said = self.run_onboard("ssh://git@10.0.0.1:30009/acme/app.git", [""])
        self.assertEqual("./main", result)
        self.assertIn("  Repos   gitea.example.com/acme/app   read", said)
        self.assertEqual({"extends": ["gitea", "basic"],
                          "git-gate": {"repos": ["gitea.example.com/acme/app"]}}, self.written())

    def test_w_makes_it_writable(self) -> None:
        self.run_onboard("ssh://git@10.0.0.1:30009/acme/app.git", ["w", ""])
        self.assertEqual({"repos": ["gitea.example.com/acme/app:write"]},
                         self.written()["git-gate"])

    def test_skip_writes_nothing(self) -> None:
        result, _ = self.run_onboard("ssh://git@10.0.0.1:30009/acme/app.git", ["s"])
        self.assertIsNone(result)
        self.assertFalse((self.project / ".bot-bottle").exists())

    def test_an_unconnected_forge_can_be_connected(self) -> None:
        def connect(url: str) -> bool:
            self.assertEqual("https://git.other.org", url)
            (self.host / "other.md").write_text(FORGE_BOTTLE.replace(
                "gitea.example.com", "git.other.org").replace("10.0.0.1:30009", "git.other.org"))
            return True
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin",
                        "git@git.other.org:acme/app.git"], check=True)
        answers = iter(["y", "", ""])
        with mock.patch("bot_bottle.approvals.trust_project_bottle"):
            onboard(self.project, self.host, ask=lambda _q: next(answers),
                    say=lambda _l: None, connect=connect)
        self.assertEqual(["other", "basic"], self.written()["extends"])

    def test_declining_to_connect_drafts_basic(self) -> None:
        result, said = self.run_onboard("https://github.com/acme/app", ["n", "w", ""])
        self.assertEqual("./main", result)
        self.assertIn("  Repos   none (github.com isn't a connected forge)", said)
        self.assertEqual({"extends": "basic"}, self.written())

    def test_no_origin(self) -> None:
        _, said = self.run_onboard(None, [""])
        self.assertIn("  Repos   none (no origin remote)", said)


class TestDraft(unittest.TestCase):
    def test_grant_needs_a_forge(self) -> None:
        with self.assertRaises(AssertionError):
            Draft(origin=None, forge=None).grant()
        self.assertTrue(callable(onboarding.read_origin))


if __name__ == "__main__":
    unittest.main()
