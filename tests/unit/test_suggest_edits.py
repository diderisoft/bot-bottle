"""Unit: a proposal's edits to other bottles (PRD 0118): the type, which
bottle an edit writes, merging it into the file, validation as if accepted,
and accepting it (a host bottle's new version, a project bottle trusted)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from bot_bottle.suggest import edits
from bot_bottle.suggest.proposal import (
    OPTION_STACK, Proposal, ProposalAdditions, ProposalCredential, ProposalEdit,
    ProposalEgress, ProposalEnv, ProposalRepo,
)
from bot_bottle.suggest.validate import validate_proposal
from tests.unit._forges import GITEA, connected

HOST_BOTTLE = """---
extends: basic
egress:
  routes:
    - host: api.example.com
      methods: [GET]
---

Example API.
"""


def _edit(bottle: str, *, hosts: tuple[str, ...] = (), grants: tuple[str, ...] = (),
          env: tuple[str, ...] = (), extends: str = "", why: str = "the why",
          forges: dict[str, dict[str, object]] | None = None) -> ProposalEdit:
    return ProposalEdit(bottle=bottle, why=why, extends=extends, forges=forges or {},
                        add=ProposalAdditions(
                            egress=[ProposalEgress(h, ["POST"]) for h in hosts],
                            repos=[ProposalRepo(g) for g in grants],
                            env=[ProposalEnv(e) for e in env]))


def _proposal(*the_edits: ProposalEdit, bottles: tuple[str, ...] = ("basic",)) -> Proposal:
    return Proposal(option=OPTION_STACK, bottles=list(bottles), new_bottle=None,
                    additions=ProposalAdditions(), similar=[], notes=[],
                    edits=list(the_edits))


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / ".bot-bottle"
        self.host = self.root / "bottles"
        self.host.mkdir(parents=True)
        (self.host / "example.md").write_text(HOST_BOTTLE)
        self.project = self.tmp / "app"
        self.project_bottles = self.project / ".bot-bottle" / "bottles"
        self.project_bottles.mkdir(parents=True)
        self.dirs = [(self.project_bottles, True), (self.host, False),
                     (BUILTIN_BOTTLES_DIR, False)]
        self.enterContext(mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root),
                                                       "HOME": str(self.tmp)}))
        self.enterContext(connected(GITEA))


class TestProposalEdits(unittest.TestCase):
    def test_round_trip(self) -> None:
        edit = _edit("./main", grants=("code.example.org/a/b:write",),
                     forges={"code.example.org": {"kind": "gitea"}})
        again = Proposal.from_dict(_proposal(edit).to_dict())
        self.assertEqual([edit], again.edits)
        self.assertEqual({"kind": "gitea"},
                         _proposal(edit).to_dict()["edits"][0]["add"]["forges"]["code.example.org"])

    def test_an_edit_that_adds_nothing_or_names_no_bottle_is_left_out(self) -> None:
        raw = _proposal().to_dict()
        raw["edits"] = [{"bottle": "x", "add": {}}, {"add": {"env": [{"name": "A"}]}},
                        {"bottle": "y", "add": {"env": [{"name": "A"}]}}]
        self.assertEqual(["y"], [e.bottle for e in Proposal.from_dict(raw).edits])
        self.assertNotIn("edits", _proposal().to_dict())


class TestTarget(_Dirs):
    def test_which_file(self) -> None:
        self.assertEqual(edits.Target(self.host / "example.md", False, True),
                         edits.target(_edit("example"), self.dirs))
        self.assertEqual(edits.Target(self.project_bottles / "main.md", True, False),
                         edits.target(_edit("./main"), self.dirs))
        self.assertEqual(edits.Target(self.host / "team.md", False, False),
                         edits.target(_edit("new:team"), self.dirs))

    def test_what_cant_be_edited(self) -> None:
        for name, why in [("basic", "built in"), ("nope", "no bottle"),
                          ("./other", "no bottle"), ("new:example", "already exists"),
                          ("new:Bad Name", "isn't a bottle name")]:
            with self.subTest(name), self.assertRaisesRegex(edits.EditError, why):
                edits.target(_edit(name), self.dirs)


class TestMerge(_Dirs):
    def test_merges_into_what_the_bottle_has(self) -> None:
        fm, _ = parse_frontmatter(HOST_BOTTLE)
        out = edits.edited_frontmatter(
            dict(fm), _edit("example", hosts=("api.example.com", "api.linear.app"),
                            grants=("gitea.example.com/acme/app:write",), env=("REGION",)), {})
        routes = {r["host"]: r for r in out["egress"]["routes"]}
        self.assertEqual([{"methods": ["GET"]}, {"methods": ["POST"]}],
                         routes["api.example.com"]["allow"])
        self.assertIn("api.linear.app", routes)
        self.assertEqual(["gitea.example.com/acme/app:write"], out["git_gate"]["repos"])
        self.assertIn("gitea.example.com", out["git_gate"]["forges"])
        self.assertEqual({"REGION": "secret:REGION"}, out["env"])
        self.assertEqual("basic", out["extends"])

    def test_diff_of_a_new_bottle(self) -> None:
        lines = edits.diff(_edit("new:team", hosts=("api.linear.app",), extends="example"),
                           self.dirs, {})
        self.assertEqual("--- /dev/null", lines[0])
        self.assertIn("+extends: example", lines)
        self.assertTrue(any("api.linear.app" in line for line in lines))


class TestValidate(_Dirs):
    def test_keeps_what_loads_and_says_what_it_dropped(self) -> None:
        kept, notes = edits.validate_edits(
            [_edit("basic", hosts=("a.example",)),
             _edit("example", hosts=("api.linear.app",), env=("LINEAR_TOKEN", "REGION"),
                   grants=("nowhere.example/a/b",)),
             _edit("./main", grants=("code.example.org/a/b:write",))],
            self.dirs, {"code.example.org": {"kind": "gitea"}})
        self.assertEqual(["example", "./main"], [e.bottle for e in kept])
        self.assertEqual(["REGION"], [v.name for v in kept[0].add.env])
        self.assertEqual([], kept[0].add.repos)
        self.assertEqual({"code.example.org": {"kind": "gitea"}}, kept[1].forges)
        self.assertTrue(any("edit of basic" in n and "built in" in n for n in notes))
        self.assertTrue(any("LINEAR_TOKEN" in n for n in notes))
        self.assertTrue(any("nowhere.example" in n for n in notes))

    def test_a_route_that_doesnt_load_drops_the_edit(self) -> None:
        bad = _edit("example")
        bad.add.credentials = []
        bad.add.egress = [ProposalEgress("api.linear.app", ["FETCH"])]
        kept, notes = edits.validate_edits([bad], self.dirs, {})
        self.assertEqual([], kept)
        self.assertIn("dropped the edit of example", notes[0])

    def test_the_stack_is_validated_as_if_accepted(self) -> None:
        proposal = _proposal(_edit("new:team", hosts=("api.linear.app",), extends="basic"),
                             bottles=("new:team",))
        proposal.additions.egress = [ProposalEgress("api.linear.app", ["POST"])]
        proposal.additions.credentials = [ProposalCredential("LINEAR_TOKEN", "api.linear.app")]
        validated, _ = validate_proposal(proposal, self.dirs)
        self.assertEqual(["team"], validated.bottles)
        self.assertEqual(1, len(validated.edits))
        # Once skipped, the stack can't name it.
        skipped, notes = validate_proposal(Proposal.from_dict(
            {**validated.to_dict(), "edits": []}), self.dirs)
        self.assertNotIn("team", skipped.bottles)
        self.assertTrue(any("dropped team from the stack" in n for n in notes))

    def test_an_unconnected_forge_is_offered_to_connect(self) -> None:
        proposal = _proposal(_edit("./main", grants=("code.example.org/a/b:write",),
                                   forges={"code.example.org": {"kind": "gitea"}}))
        validated, _ = validate_proposal(proposal, self.dirs)
        self.assertEqual({"kind": "gitea"}, validated.forges["code.example.org"])
        self.assertEqual(["code.example.org"], edits.unconnected(validated.edits[0]))


class TestAccept(_Dirs):
    def test_a_host_bottle_gets_a_new_version(self) -> None:
        with mock.patch("bot_bottle.manifest.versions.record_or_warn") as record:
            where = edits.accept(_edit("example", hosts=("api.linear.app",)), self.dirs, {})
        self.assertTrue(where.exists)
        self.assertIn("api.linear.app", (self.host / "example.md").read_text())
        self.assertIn("Example API.", (self.host / "example.md").read_text())
        record.assert_called_once_with("architect: edit example", root=self.root)

    def test_a_project_bottle_is_written_and_trusted(self) -> None:
        with mock.patch("bot_bottle.approvals.trust_project_bottle") as trust:
            where = edits.accept(_edit("./main", grants=("gitea.example.com/acme/app:write",)),
                                 self.dirs, {})
        self.assertTrue(where.project)
        text = (self.project_bottles / "main.md").read_text()
        self.assertIn("gitea.example.com/acme/app:write", text)
        trust.assert_called_once_with(self.project, "main")

    def test_a_grant_on_an_unconnected_forge_waits_for_it(self) -> None:
        with self.assertRaisesRegex(edits.EditError, "connect code.example.org first"):
            edits.accept(_edit("./main", grants=("code.example.org/a/b:write",)), self.dirs, {})
        self.assertFalse((self.project_bottles / "main.md").exists())

    def test_summary(self) -> None:
        self.assertEqual("+ api.linear.app · + acme/app (push) · + REGION",
                         edits.summary(_edit("x", hosts=("api.linear.app",), env=("REGION",),
                                             grants=("gitea.example.com/acme/app:write",))))


if __name__ == "__main__":
    unittest.main()
