"""Unit: grants on forges this host hasn't connected yet (`suggest.forges`,
PRD 0118): which forges are known, validation keeping their grants, the
bottle carrying their descriptions, and what the architect is told."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.onboarding import Origin
from bot_bottle.suggest import forges
from bot_bottle.suggest.architect import _build_prompt, _forges, _parse_and_repair
from bot_bottle.suggest.proposal import (
    OPTION_STACK_ADDITIONS, Proposal, ProposalAdditions, ProposalRepo,
)
from bot_bottle.suggest.review import glance
from bot_bottle.suggest.validate import proposal_to_frontmatter, validate_proposal
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from tests.unit._forges import GITEA, connected

DESCRIBES = """---
extends: basic
git_gate:
  forges:
    code.example.org:
      kind: gitea
      url: https://code.example.org/git
---

Describes a forge.
"""


def _proposal(*grants: str) -> Proposal:
    return Proposal(option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
                    additions=ProposalAdditions(repos=[ProposalRepo(g, "push") for g in grants]),
                    similar=[], notes=[])


class TestDescribed(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.bottles = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.bottles / "team.md").write_text(DESCRIBES)
        (self.bottles / "broken.md").write_text("---\ngit-gate: [\n---\n")
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def test_a_bottles_forge_until_its_connected(self) -> None:
        with connected():
            self.assertEqual({"code.example.org": {"kind": "gitea",
                                                   "url": "https://code.example.org/git"}},
                             forges.described_forges(self.dirs))
        conn = mock.Mock()
        with mock.patch("bot_bottle.forge_connections.connections",
                        new=lambda: {"code.example.org": conn}):
            self.assertEqual({}, forges.described_forges(self.dirs))

    def test_the_projects_origin(self) -> None:
        origin = Origin(host="gitea.dideric.is", port="", owner="a", repo="b")
        with connected(), \
                mock.patch("bot_bottle.onboarding.read_origin", return_value=origin), \
                mock.patch("bot_bottle.onboarding.find_forge", return_value=None), \
                mock.patch("bot_bottle.forge_kinds.detect", return_value="gitea") as detect:
            found = forges.described_forges([], Path("/p"))
        self.assertEqual({"gitea.dideric.is": {"kind": "gitea"}}, found)
        detect.assert_called_once_with("https://gitea.dideric.is")
        on_an_address = Origin(host="192.0.2.10", port="30009", owner="a", repo="b", ssh=True)
        with connected(), \
                mock.patch("bot_bottle.onboarding.read_origin", return_value=on_an_address), \
                mock.patch("bot_bottle.onboarding.find_forge", return_value=None):
            self.assertEqual({}, forges.described_forges([], Path("/p")))

    def test_grant_forge_and_connect_url(self) -> None:
        self.assertEqual("gitea.example.com", forges.grant_forge("gitea.example.com/a/b:write"))
        self.assertEqual("", forges.grant_forge("not a grant"))
        self.assertEqual("https://h", forges.connect_url("h", {}))
        self.assertEqual("https://h/git", forges.connect_url("h", {"url": "https://h/git/"}))


class TestValidation(unittest.TestCase):
    def test_a_grant_on_a_described_forge_is_kept_with_its_description(self) -> None:
        described = {"gitea.example.com": {"kind": "gitea"}}
        with connected():
            p, notes = validate_proposal(_proposal("gitea.example.com/acme/app:write",
                                                   "nowhere.example.net/a/b"),
                                         described=described)
        self.assertEqual(["gitea.example.com/acme/app:write"], [r.grant for r in p.additions.repos])
        self.assertTrue(any("nowhere.example.net" in n for n in notes))
        self.assertEqual(described, p.forges)
        fm = proposal_to_frontmatter(p)
        self.assertEqual({"gitea.example.com": {"kind": "gitea"}}, fm["git_gate"]["forges"])

    def test_once_connected_it_loads_as_any_grant(self) -> None:
        p = _proposal("gitea.example.com/acme/app:write")
        p.forges = {"gitea.example.com": {"kind": "gitea"}}
        with connected(GITEA), mock.patch.dict("os.environ", {"GITEA_X": "x"}):
            again, _ = validate_proposal(p)
            fm = proposal_to_frontmatter(again)
        self.assertEqual({}, again.forges)
        self.assertEqual(1, len(again.additions.repos))
        self.assertEqual("gitea", fm["git_gate"]["forges"]["gitea.example.com"]["kind"])

    def test_the_glance_offers_to_connect_it(self) -> None:
        p = _proposal("gitea.example.com/acme/app:write")
        p.forges = {"gitea.example.com": {"kind": "gitea", "url": "https://gitea.example.com/x"}}
        with connected():
            seen = glance(p, [(BUILTIN_BOTTLES_DIR, False)])
        assert seen is not None
        self.assertEqual({"gitea.example.com": "https://gitea.example.com/x"}, seen.connect)
        self.assertEqual([], seen.warns)


class TestProposal(unittest.TestCase):
    def test_forges_round_trip(self) -> None:
        p = _proposal()
        self.assertNotIn("forges", p.to_dict())
        p.forges = {"h.example.com": {"kind": "gitea"}}
        self.assertEqual(p.forges, Proposal.from_dict(p.to_dict()).forges)

    def test_the_model_cant_set_them(self) -> None:
        raw = json.dumps({**_proposal().to_dict(), "forges": {"evil.example": {"kind": "gitea"}}})
        parsed = _parse_and_repair(raw)
        assert parsed is not None
        self.assertEqual({}, parsed.forges)


class TestArchitect(unittest.TestCase):
    def test_it_sees_unconnected_forges(self) -> None:
        with connected(GITEA):
            found = _forges({"code.example.org": {"kind": "gitea"},
                             "gitea.example.com": {"kind": "gitea"}})
        self.assertEqual([("gitea.example.com", True), ("code.example.org", False)],
                         [(f["name"], f["connected"]) for f in found])
        prompt = _build_prompt("t", [], {}, found, [])
        self.assertIn("connected: false", prompt)
        self.assertIn("don't leave the repo out", prompt)


if __name__ == "__main__":
    unittest.main()
