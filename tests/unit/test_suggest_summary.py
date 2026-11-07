"""Unit: a proposal's one-sentence summary and what it adds, counted (the
guided start's review)."""

from __future__ import annotations

import unittest
from pathlib import Path

from bot_bottle.suggest import render
from bot_bottle.suggest.architect import _build_prompt  # pylint: disable=protected-access
from bot_bottle.suggest.proposal import (
    OPTION_NEW_EXTENDING,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalEgress,
    ProposalNewBottle,
    ProposalRepo,
)
from bot_bottle.suggest.validate import validate_proposal


def _proposal(**kw: object) -> Proposal:
    fields: dict[str, object] = {
        "option": OPTION_STACK_ADDITIONS, "bottles": ["basic", "gitea"], "new_bottle": None,
        "additions": ProposalAdditions(), "similar": [], "notes": []}
    fields.update(kw)
    return Proposal(**fields)  # type: ignore[arg-type]


class TestTheField(unittest.TestCase):
    def test_parsed_on_one_line_and_kept_when_set(self) -> None:
        p = Proposal.from_dict({"option": "reuse", "summary": "Reuse  basic;\nit has all."})
        self.assertEqual("Reuse basic; it has all.", p.summary)
        self.assertEqual("Reuse basic; it has all.", p.to_dict()["summary"])

    def test_absent_or_not_text_is_empty_and_not_written(self) -> None:
        self.assertEqual("", Proposal.from_dict({"option": "reuse", "summary": 3}).summary)
        self.assertNotIn("summary", Proposal.from_dict({"option": "reuse"}).to_dict())

    def test_the_architect_is_asked_for_one(self) -> None:
        prompt = _build_prompt("t", [], {}, [], [])
        self.assertIn('"summary" key', prompt)
        self.assertIn("five words or fewer", prompt)
        self.assertIn("Don't name hosts, methods or access", prompt)


class TestValidation(unittest.TestCase):
    def test_kept_when_nothing_is_dropped(self) -> None:
        p = _proposal(bottles=["basic"], summary="Reuse basic.")
        self.assertEqual("Reuse basic.", validate_proposal(p)[0].summary)

    def test_cleared_when_something_is_dropped(self) -> None:
        p = _proposal(bottles=["basic"], summary="Add the API.",
                      additions=ProposalAdditions(egress=[ProposalEgress(host="", methods=[])]))
        self.assertEqual("", validate_proposal(p)[0].summary)


class TestSummaryLine(unittest.TestCase):
    def test_the_architects_sentence_first(self) -> None:
        self.assertEqual("Stack gitea.", render.summary_line(_proposal(summary="Stack gitea.")))

    def test_made_from_a_stack_and_what_it_adds(self) -> None:
        p = _proposal(additions=ProposalAdditions(
            egress=[ProposalEgress("a.example.com"), ProposalEgress("b.example.com")],
            repos=[ProposalRepo("gitea/acme/app:write")]))
        self.assertEqual("Run in basic + gitea, adding 2 hosts and 1 repo.",
                         render.summary_line(p))
        self.assertEqual("Run in basic.", render.summary_line(_proposal(bottles=[])))

    def test_made_from_a_new_bottle(self) -> None:
        p = _proposal(option=OPTION_NEW_EXTENDING, bottles=[], new_bottle=ProposalNewBottle(
            "weather", extends="basic", about="Forecast APIs."))
        self.assertEqual("A new bottle, weather, extending basic: Forecast APIs.",
                         render.summary_line(p))


class TestCounts(unittest.TestCase):
    def test_added_counts(self) -> None:
        p = _proposal(additions=ProposalAdditions(egress=[ProposalEgress("a")]))
        self.assertEqual(["1 host"], render.added_counts(p))

    def test_the_rows_of_a_real_stack(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.suggest.flow import bottle_dirs
        dirs = bottle_dirs(Path.cwd(), Path("/nowhere"))
        p = _proposal(bottles=["basic"], additions=ProposalAdditions(
            egress=[ProposalEgress("api.example.com", ["GET"])]))
        rows = render.proposal_elements(p, dirs)
        assert rows is not None
        self.assertIn("api.example.com", [r["name"] for r in rows])


if __name__ == "__main__":
    unittest.main()
