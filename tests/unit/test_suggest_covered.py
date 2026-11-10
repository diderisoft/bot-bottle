"""suggest.covered: additions the proposal's own bottles already cover are
dropped, with a note naming the bottle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.manifest.egress import ManifestEgressRoute, ManifestMatchEntry, ManifestPathMatch
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from bot_bottle.suggest import covered
from bot_bottle.suggest.proposal import (
    OPTION_NEW_EXTENDING,
    OPTION_STACK,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
    ProposalEnv,
    ProposalNewBottle,
    ProposalRepo,
)
from bot_bottle.suggest.validate import validate_proposal


def _proposal(bottles: list[str], additions: ProposalAdditions, *,
              option: str = OPTION_STACK_ADDITIONS,
              new_bottle: ProposalNewBottle | None = None) -> Proposal:
    return Proposal(option=option, bottles=bottles, new_bottle=new_bottle,
                    additions=additions, similar=[], notes=[])


class TestDropCovered(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.bottles = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.bottles / "web.md").write_text(
            "---\nenv:\n  NODE_ENV: development\negress:\n  routes:\n"
            "    - host: api.example.com\n      methods: [\"*\"]\n---\n\nA bottle with an env var.\n")
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def test_a_route_basic_already_allows_is_dropped_with_a_note(self) -> None:
        p = _proposal(["basic"], ProposalAdditions(egress=[
            ProposalEgress("api.open-meteo.com", ["GET"]),
            ProposalEgress("registry.npmjs.org", ["GET"])]))
        out, notes = validate_proposal(p, self.dirs)
        self.assertEqual(["api.open-meteo.com"], [e.host for e in out.additions.egress])
        self.assertIn("dropped 1 addition already covered by basic: egress registry.npmjs.org",
                      notes)
        self.assertEqual(notes, out.notes)

    def test_a_wider_route_or_one_with_a_credential_is_kept(self) -> None:
        p = _proposal(["basic"], ProposalAdditions(
            egress=[ProposalEgress("pypi.org", ["POST"]),
                    ProposalEgress("registry.npmjs.org", ["GET"])],
            credentials=[ProposalCredential("NPM_TOKEN", "registry.npmjs.org")]))
        out = covered.drop_covered(p, self.dirs)
        self.assertEqual(["pypi.org", "registry.npmjs.org"],
                         [e.host for e in out.additions.egress])
        self.assertEqual([], out.notes)

    def test_methods_left_unset_need_a_route_that_allows_everything(self) -> None:
        any_method = _proposal(["web"], ProposalAdditions(egress=[
            ProposalEgress("api.example.com")]))
        self.assertEqual([], covered.drop_covered(any_method, self.dirs).additions.egress)
        p = _proposal(["basic"], ProposalAdditions(egress=[ProposalEgress("pypi.org")]))
        self.assertEqual(["pypi.org"],
                         [e.host for e in covered.drop_covered(p, self.dirs).additions.egress])

    def test_env_set_by_a_base_bottle_is_dropped_and_names_it(self) -> None:
        p = _proposal(["basic", "web"], ProposalAdditions(env=[ProposalEnv("NODE_ENV"),
                                                               ProposalEnv("PORT")]))
        out = covered.drop_covered(p, self.dirs)
        self.assertEqual(["PORT"], [v.name for v in out.additions.env])
        self.assertEqual(["dropped 1 addition already covered by web: env NODE_ENV"], out.notes)

    def test_nothing_left_turns_stack_additions_into_stack_or_reuse(self) -> None:
        two = _proposal(["basic", "web"], ProposalAdditions(env=[ProposalEnv("NODE_ENV")]))
        self.assertEqual(OPTION_STACK, covered.drop_covered(two, self.dirs).option)
        one = _proposal(["web"], ProposalAdditions(env=[ProposalEnv("NODE_ENV")]))
        self.assertEqual("reuse", covered.drop_covered(one, self.dirs).option)

    def test_a_new_bottle_is_checked_against_what_it_extends(self) -> None:
        p = _proposal([], ProposalAdditions(egress=[ProposalEgress("pypi.org", ["GET"])]),
                      option=OPTION_NEW_EXTENDING,
                      new_bottle=ProposalNewBottle(name="x", extends="basic"))
        self.assertEqual([], covered.drop_covered(p, self.dirs).additions.egress)
        scratch = _proposal([], ProposalAdditions(), option="new",
                            new_bottle=ProposalNewBottle(name="x"))
        self.assertEqual([], covered.base_bottles(scratch))

    def test_a_base_that_does_not_load_leaves_the_proposal_alone(self) -> None:
        p = _proposal(["missing"], ProposalAdditions(egress=[ProposalEgress("pypi.org", ["GET"])]))
        self.assertEqual(["pypi.org"],
                         [e.host for e in covered.drop_covered(p, self.dirs).additions.egress])

    def test_repo_grants_need_the_same_repo_with_enough_access(self) -> None:
        base = {("acme", "app"): False, ("acme", "lib"): True}
        self.assertTrue(covered.repo_covered("gitea.example.com/acme/app", base))
        self.assertFalse(covered.repo_covered("gitea.example.com/acme/app:write", base))
        self.assertTrue(covered.repo_covered("lib2=gitea.example.com/acme/lib:write", base))
        self.assertFalse(covered.repo_covered("gitea.example.com/acme/other", base))
        self.assertFalse(covered.repo_covered("not a grant", base))
        p = _proposal(["basic"], ProposalAdditions(repos=[ProposalRepo("gitea.example.com/a/b")]))
        self.assertEqual(1, len(covered.drop_covered(p, self.dirs).additions.repos))

    def test_allowed_methods_ignores_rules_limited_to_some_paths(self) -> None:
        route = ManifestEgressRoute(Host="h", Matches=(
            ManifestMatchEntry(Methods=("GET",)),
            ManifestMatchEntry(Paths=(ManifestPathMatch(Type="prefix", Value="/x"),),
                               Methods=("POST",))))
        self.assertEqual(frozenset({"GET"}), covered.allowed_methods(route))
        self.assertEqual(covered.ALL_METHODS,
                         covered.allowed_methods(ManifestEgressRoute(Host="h", Inspect=False)))


if __name__ == "__main__":
    unittest.main()
