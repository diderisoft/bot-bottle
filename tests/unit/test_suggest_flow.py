"""Tests for the one suggestion flow (PRD 0117): architect first, the rules
path when it can't answer, and the proposal saying which answered."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import suggest as suggest_pkg
from bot_bottle.suggest import flow
from bot_bottle.suggest.architect import ArchitectError
from bot_bottle.suggest.proposal import (
    OPTION_REUSE,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalEgress,
)


def _architect_answer(*_: object, **__: object) -> Proposal:
    return Proposal(option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
                    additions=ProposalAdditions(egress=[ProposalEgress(host="api.example.com")]),
                    similar=[], notes=["from the model"])


class TestSuggestFlow(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home),
                                           "BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_the_architect_answers_first(self) -> None:
        with mock.patch.object(flow, "architect_suggest", side_effect=_architect_answer):
            p = flow.suggest("call the example api", self.cwd, host_root=self.root)
        self.assertEqual((flow.SOURCE_ARCHITECT, "api.example.com"),
                         (p.source, p.additions.egress[0].host))
        self.assertEqual("architect", p.to_dict()["source"])

    def test_the_rules_answer_when_the_architect_cant_and_say_why(self) -> None:
        with mock.patch.object(flow, "architect_suggest",
                               side_effect=ArchitectError("claude isn't installed on this host")):
            p = flow.suggest("do a thing", self.cwd, host_root=self.root)
        self.assertEqual((flow.SOURCE_RULES, OPTION_REUSE, ["basic"]),
                         (p.source, p.option, p.bottles))
        self.assertTrue(any("bottle-architect didn't answer (claude isn't installed" in n
                            for n in p.notes), p.notes)

    def test_rules_skips_the_architect(self) -> None:
        with mock.patch.object(flow, "architect_suggest") as arch:
            p = flow.suggest("do a thing", self.cwd, rules=True, host_root=self.root)
        arch.assert_not_called()
        self.assertEqual(flow.SOURCE_RULES, p.source)
        self.assertFalse(any("didn't answer" in n for n in p.notes))

    def test_without_a_project_the_proposal_says_so(self) -> None:
        with mock.patch.object(flow, "architect_suggest", side_effect=_architect_answer) as arch:
            p = flow.suggest("do a thing", None, host_root=self.root)
        self.assertIsNone(arch.call_args.args[1])
        self.assertEqual(flow.NO_PROJECT_NOTE, p.notes[0])
        self.assertEqual([(self.root / "bottles", False)], flow.bottle_dirs(None, self.root)[:1])

    def test_the_package_entry_point_is_the_flow(self) -> None:
        with mock.patch.object(flow, "architect_suggest", side_effect=_architect_answer):
            p = suggest_pkg.suggest("t", self.cwd, host_root=self.root)
        self.assertEqual(flow.SOURCE_ARCHITECT, p.source)


if __name__ == "__main__":
    unittest.main()
