"""Bottle onboarding (PRD 0103)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bot_bottle import onboarding
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.forge_connections import ForgeConnectionError
from bot_bottle.onboarding import Draft, ForgeMatch, Origin, find_forge, onboard, parse_origin
from tests.unit._forges import GITEA

# How a bottle describes the connected forge: its SSH address is this
# machine's (an IP), so it isn't described.
MATCH = ForgeMatch("gitea.example.com", (("kind", "gitea"),))


class TestOrigin(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(Origin("10.0.0.1", "30009", "acme", "app", ssh=True),
                         parse_origin("ssh://git@10.0.0.1:30009/acme/app.git"))
        self.assertEqual(Origin("gitea.example.com", "", "acme", "app", ssh=True),
                         parse_origin("git@gitea.example.com:acme/app.git"))
        self.assertEqual(Origin("github.com", "", "acme", "app"),
                         parse_origin("https://github.com/acme/app"))
        self.assertIsNone(parse_origin("https://github.com/acme"))
        self.assertIsNone(parse_origin("/some/local/path"))


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.found = {GITEA.host: GITEA}
        self.enterContext(mock.patch("bot_bottle.forge_connections.connections",
                                     new=lambda: dict(self.found)))
        self.project = tmp / "app"
        self.project.mkdir()


class TestFindForge(_Dirs):
    def test_by_ssh_or_web_address(self) -> None:
        self.assertEqual(MATCH, find_forge(Origin("10.0.0.1", "30009", "a", "b")))
        self.assertEqual(MATCH, find_forge(Origin("gitea.example.com", "", "a", "b")))
        self.assertIsNone(find_forge(Origin("10.0.0.1", "22", "a", "b")))
        self.assertIsNone(find_forge(Origin("github.com", "", "a", "b")))
        with mock.patch("bot_bottle.forge_connections.connections",
                        side_effect=ForgeConnectionError("bad")):
            self.assertIsNone(find_forge(Origin("gitea.example.com", "", "a", "b")))

    def test_an_ssh_origin_on_the_web_host_matches_whatever_its_port(self) -> None:
        """`ssh://git@gitea.example.com:30009/…` when the forge's SSH address
        is an IP: its port is the SSH one, not the web URL's, so it matches the
        web host. An https origin still has to agree on the port."""
        origin = parse_origin("ssh://git@gitea.example.com:30009/acme/app.git")
        https = parse_origin("https://gitea.example.com:8443/acme/app.git")
        assert origin is not None and https is not None
        self.assertEqual(MATCH, find_forge(origin))
        self.assertIsNone(find_forge(https))


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
            result = onboard(self.project, ask=lambda _q: next(answers),
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
        self.assertEqual({"extends": "basic",
                          "git-gate": {"forges": {"gitea.example.com": {"kind": "gitea"}},
                                       "repos": ["gitea.example.com/acme/app"]}}, self.written())

    def test_w_makes_it_writable(self) -> None:
        self.run_onboard("ssh://git@10.0.0.1:30009/acme/app.git", ["w", ""])
        self.assertEqual(["gitea.example.com/acme/app:write"],
                         self.written()["git-gate"]["repos"])  # type: ignore[index]

    def test_skip_writes_nothing(self) -> None:
        result, _ = self.run_onboard("ssh://git@10.0.0.1:30009/acme/app.git", ["s"])
        self.assertIsNone(result)
        self.assertFalse((self.project / ".bot-bottle").exists())

    def test_an_unconnected_forge_can_be_connected(self) -> None:
        def connect(url: str) -> bool:
            self.assertEqual("https://git.other.org", url)
            self.found["git.other.org"] = replace(
                GITEA, host="git.other.org", url="https://git.other.org",
                ssh="ssh://git@git.other.org:2222")
            return True
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin",
                        "git@git.other.org:acme/app.git"], check=True)
        answers = iter(["y", "", ""])
        with mock.patch("bot_bottle.approvals.trust_project_bottle"):
            onboard(self.project, ask=lambda _q: next(answers),
                    say=lambda _l: None, connect=connect)
        self.assertEqual({"forges": {"git.other.org": {"kind": "gitea",
                                                        "ssh": "ssh://git@git.other.org:2222"}},
                          "repos": ["git.other.org/acme/app"]}, self.written()["git-gate"])

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
