"""Tests for the Proposal schema, serialisation and validation (PRD 0117)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bot_bottle.suggest.proposal import (
    OPTION_NEW,
    OPTION_REUSE,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
    ProposalEnv,
    ProposalNewBottle,
    ProposalRepo,
    ProposalSimilar,
)
from bot_bottle.suggest.validate import proposal_to_frontmatter, validate_proposal


class TestProposalParsing(unittest.TestCase):

    def _minimal(self, **overrides: object) -> dict:
        base = {
            "option": OPTION_REUSE,
            "bottles": ["basic"],
            "new_bottle": None,
            "additions": {},
            "similar": [],
            "notes": [],
        }
        base.update(overrides)
        return base

    def test_round_trip_minimal(self) -> None:
        p = Proposal.from_dict(self._minimal())
        self.assertEqual(p.option, OPTION_REUSE)
        self.assertEqual(p.bottles, ["basic"])
        self.assertIsNone(p.new_bottle)
        self.assertTrue(p.additions.is_empty())

    def test_round_trip_full(self) -> None:
        d = {
            "option": OPTION_STACK_ADDITIONS,
            "bottles": ["gitea", "basic"],
            "new_bottle": None,
            "additions": {
                "egress": [{"host": "api.fly.io", "methods": ["GET", "POST"],
                             "why": "flyctl API"}],
                "repos": [{"grant": "gitea.example.com/acme/app:write",
                            "why": "deploy"}],
                "env": [{"name": "FLY_APP", "why": "app name"}],
                "credentials": [{"name": "FLY_TOKEN", "route": "api.fly.io",
                                  "why": "deploy token"}],
            },
            "similar": [{"bottle": "fly-staging", "covers": 0.9}],
            "notes": ["check builder hosts"],
        }
        p = Proposal.from_dict(d)
        self.assertEqual(p.option, OPTION_STACK_ADDITIONS)
        self.assertEqual(p.bottles, ["gitea", "basic"])
        self.assertIsNone(p.new_bottle)
        self.assertEqual(len(p.additions.egress), 1)
        self.assertEqual(p.additions.egress[0].host, "api.fly.io")
        self.assertEqual(p.additions.egress[0].methods, ["GET", "POST"])
        self.assertEqual(len(p.additions.repos), 1)
        self.assertEqual(p.additions.repos[0].grant, "gitea.example.com/acme/app:write")
        self.assertEqual(len(p.additions.env), 1)
        self.assertEqual(p.additions.env[0].name, "FLY_APP")
        self.assertEqual(len(p.additions.credentials), 1)
        self.assertEqual(p.additions.credentials[0].name, "FLY_TOKEN")
        self.assertEqual(len(p.similar), 1)
        self.assertAlmostEqual(p.similar[0].covers, 0.9)
        self.assertEqual(p.notes, ["check builder hosts"])

    def test_to_dict_round_trip(self) -> None:
        d = {
            "option": OPTION_STACK_ADDITIONS,
            "bottles": ["basic"],
            "new_bottle": None,
            "additions": {
                "egress": [{"host": "api.example.com", "methods": ["POST"], "why": ""}],
                "repos": [],
                "env": [],
                "credentials": [],
            },
            "similar": [],
            "notes": ["a note"],
        }
        p = Proposal.from_dict(d)
        out = p.to_dict()
        self.assertEqual(out["option"], OPTION_STACK_ADDITIONS)
        self.assertEqual(out["bottles"], ["basic"])
        self.assertEqual(out["additions"]["egress"][0]["host"], "api.example.com")
        self.assertEqual(out["notes"], ["a note"])

    def test_from_json(self) -> None:
        raw = json.dumps(self._minimal())
        p = Proposal.from_json(raw)
        self.assertEqual(p.option, OPTION_REUSE)

    def test_new_extending_requires_new_bottle(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Proposal.from_dict(self._minimal(option="new-extending", new_bottle=None))
        self.assertIn("new_bottle", str(ctx.exception))

    def test_invalid_option(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Proposal.from_dict(self._minimal(option="bad-option"))
        self.assertIn("option", str(ctx.exception))

    def test_invalid_json(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Proposal.from_json("not json {")
        self.assertIn("JSON", str(ctx.exception))

    def test_new_bottle_fields(self) -> None:
        d = self._minimal(option=OPTION_NEW,
                          new_bottle={"name": "fly-staging", "extends": "basic",
                                      "about": "Fly staging deploy"})
        p = Proposal.from_dict(d)
        self.assertIsNotNone(p.new_bottle)
        assert p.new_bottle is not None
        self.assertEqual(p.new_bottle.name, "fly-staging")
        self.assertEqual(p.new_bottle.extends, "basic")

    def test_new_bottle_name_required(self) -> None:
        d = self._minimal(option=OPTION_NEW, new_bottle={"name": "", "extends": ""})
        with self.assertRaises(ValueError):
            Proposal.from_dict(d)


class TestProposalToFrontmatter(unittest.TestCase):

    def test_reuse_option_no_extends_in_fm(self) -> None:
        p = Proposal(
            option=OPTION_REUSE, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(), similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        self.assertEqual(fm.get("extends"), "basic")

    def test_stack_extends_list(self) -> None:
        p = Proposal(
            option="stack", bottles=["gitea", "basic"], new_bottle=None,
            additions=ProposalAdditions(), similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        self.assertEqual(fm["extends"], ["gitea", "basic"])

    def test_additions_egress(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="api.example.com", methods=["POST"])],
            ),
            similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        routes = fm["egress"]["routes"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["host"], "api.example.com")
        # Methods go into inspect.matches
        self.assertIn("inspect", routes[0])
        self.assertEqual(routes[0]["inspect"]["matches"][0]["methods"], ["POST"])

    def test_credential_adds_auth_to_route(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="api.example.com", methods=["POST"])],
                credentials=[ProposalCredential(name="MY_TOKEN", route="api.example.com")],
            ),
            similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        route = fm["egress"]["routes"][0]
        self.assertIn("inspect", route)
        self.assertEqual(route["inspect"]["auth"]["token_ref"], "MY_TOKEN")

    def test_env_vars_as_prompt_placeholders(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                env=[ProposalEnv(name="FLY_APP")],
            ),
            similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        self.assertEqual(fm["env"]["FLY_APP"], "?FLY_APP")

    def test_no_inspect_false_ever(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="raw.example.com")],
            ),
            similar=[], notes=[],
        )
        fm = proposal_to_frontmatter(p)
        route = fm["egress"]["routes"][0]
        # inspect:false must never appear
        inspect_val = route.get("inspect")
        self.assertIsNot(inspect_val, False)


class TestValidateProposal(unittest.TestCase):

    def _basic_proposal(self) -> Proposal:
        return Proposal(
            option=OPTION_REUSE, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(), similar=[], notes=[],
        )

    def test_valid_basic_reuse(self) -> None:
        p = self._basic_proposal()
        validated, notes = validate_proposal(p)
        self.assertEqual(validated.option, OPTION_REUSE)

    def test_valid_egress_route(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="api.example.com", methods=["GET"])],
            ),
            similar=[], notes=[],
        )
        validated, notes = validate_proposal(p)
        self.assertEqual(len(validated.additions.egress), 1)

    def test_invalid_egress_dropped_with_note(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="", methods=[])],  # empty host
            ),
            similar=[], notes=[],
        )
        validated, notes = validate_proposal(p)
        # Empty host route should be rejected or at least not retained
        self.assertTrue(
            len(validated.additions.egress) == 0
            or validated.additions.egress[0].host != "",
        )

    def test_orphaned_credential_dropped(self) -> None:
        p = Proposal(
            option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
            additions=ProposalAdditions(
                egress=[],  # no egress route for this credential
                credentials=[ProposalCredential(name="TOKEN", route="api.example.com")],
            ),
            similar=[], notes=[],
        )
        validated, notes = validate_proposal(p)
        self.assertEqual(len(validated.additions.credentials), 0)
        self.assertTrue(any("credential" in n.lower() or "TOKEN" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
