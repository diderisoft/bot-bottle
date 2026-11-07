"""Unit: the guided start's review card (`suggest.review`, `review_card`)."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from bot_bottle.cli import screen
from bot_bottle.cli.commands import review_card
from bot_bottle.forge_connections import ForgeConnectionError, ForgeUser
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from bot_bottle.suggest import review
from bot_bottle.suggest.proposal import (
    OPTION_NEW_EXTENDING,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
    ProposalEnv,
    ProposalNewBottle,
    ProposalRepo,
)
from bot_bottle.suggest.review import Line, repo_names, route_methods, short_methods
from tests.unit._forges import GITEA, connected

PLAIN = screen.Style(color=False)

TEAM = """---
extends: basic
egress:
  routes:
    - host: api.example.com
      inspect:
        auth: {scheme: Bearer, token_ref: API_TOKEN}
    - host: pypi.org
      inspect: false
    - host: crates.io
      blocked: true
git-gate:
  repos:
    - gitea.example.com/acme/app:write
    - gitea.example.com/acme/web:write
    - gitea.example.com/acme/docs
env:
  A: "1"
  B: "2"
  C: "3"
  D: "4"
---

The team's services.
"""


def _proposal(**kw: object) -> Proposal:
    fields: dict[str, object] = {
        "option": OPTION_STACK_ADDITIONS, "bottles": ["team"], "new_bottle": None,
        "additions": ProposalAdditions(), "similar": [], "notes": []}
    fields.update(kw)
    return Proposal(**fields)  # type: ignore[arg-type]


class TestMethods(unittest.TestCase):
    def test_route_methods(self) -> None:
        cases: list[tuple[dict[str, object], set[str]]] = [
            ({"host": "h", "blocked": True}, set()),
            ({"host": "h", "inspect": False}, {"any"}),
            ({"host": "h"}, {"GET"}),
            ({"host": "h", "inspect": {"auth": {"token_ref": "T"}}}, {"any"}),
            ({"host": "h", "inspect": {"preserve_auth": True}}, {"any"}),
            ({"host": "h", "inspect": {"matches": [{"methods": ["get", "HEAD"]},
                                                   {"methods": ["POST"]}]}},
             {"GET", "HEAD", "POST"}),
            ({"host": "h", "inspect": {"matches": [{"methods": ["GET"]}, {"paths": []}]}},
             {"any"}),
        ]
        for route, methods in cases:
            with self.subTest(route=route):
                self.assertEqual(methods, route_methods(route))

    def test_short_methods(self) -> None:
        self.assertEqual("GET", short_methods({"GET", "HEAD", "OPTIONS"}))
        self.assertEqual("GET, POST, PATCH", short_methods({"PATCH", "GET", "POST", "HEAD"}))
        self.assertEqual("HEAD", short_methods({"HEAD"}))
        self.assertEqual("any", short_methods({"GET", "POST", "PUT", "DELETE"}))
        self.assertEqual("any", short_methods({"any", "GET"}))
        self.assertEqual("any", short_methods(set()))

    def test_repo_names(self) -> None:
        self.assertEqual("a/x, a/y", repo_names(["a/x", "a/y"]))
        self.assertEqual("didericis/* ×3, acme/app",
                         repo_names(["didericis/a", "didericis/b", "acme/app", "didericis/c"]))
        many = [f"o{i}/r" for i in range(5)]
        self.assertEqual("o0/r, o1/r +3", repo_names(many))
        self.assertEqual("x, y +1", repo_names(["x", "y", "z"]))


class TestReview(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))  # pylint: disable=consider-using-with
        self.bottles = self.tmp / "bottles"
        self.bottles.mkdir()
        (self.bottles / "team.md").write_text(TEAM)
        self.dirs = [(self.bottles, False), (BUILTIN_BOTTLES_DIR, False)]

    def lines(self, proposal: Proposal) -> list[Line]:
        found = review.review(proposal, self.dirs)
        assert found is not None
        return found

    def test_new_then_each_bottle_then_forges(self) -> None:
        add = ProposalAdditions(
            egress=[ProposalEgress("api.linear.app", ["GET", "POST"], "read tickets")],
            repos=[ProposalRepo("gitea.example.com/acme/ops:write", "label issues")],
            env=[ProposalEnv("MODE", "the mode")],
            credentials=[ProposalCredential("LINEAR_TOKEN", "api.linear.app")])
        with connected(GITEA):
            lines = self.lines(_proposal(additions=add))
        got = [(x.label, x.what, x.detail, x.access, x.secret, x.tone) for x in lines]
        self.assertEqual([
            ("new", "api.linear.app", "read tickets", "GET, POST", "$LINEAR_TOKEN", "new"),
            ("", "acme/ops", "label issues", "push", "", "new"),
            ("", "MODE", "the mode", "env", "", "new"),
            ("team", "2 hosts", "The team's services", "any", "$API_TOKEN", "write"),
            ("", "2 repos", "acme/app, acme/web", "push", "", "write"),
            ("", "1 repo", "acme/docs", "read", "", "read"),
            ("", "4 env vars", "A, B, C +1", "", "", "read"),
        ], got[:7])
        basic = got[7]
        self.assertEqual(("basic", "GET", "read"), (basic[0], basic[3], basic[5]))
        self.assertTrue(basic[1].endswith("hosts"))
        self.assertEqual("Package registries and public code, read-only", basic[2])
        self.assertEqual([("forge", "gitea.example.com", "as bot")],
                         [(x.label, x.what, x.detail) for x in lines[8:]])

    def test_nearest_bottle_counts_a_host_and_a_blocked_one_goes(self) -> None:
        with connected(GITEA):
            team, = [x for x in self.lines(_proposal()) if x.label == "team"]
            every = review.every_line(_proposal(), self.dirs)
        assert every is not None
        self.assertEqual("2 hosts", team.what)  # pypi.org is team's, not basic's
        basic_hosts = [x.what for x in every if x.label == "basic"] + [
            x.what for x in every[every.index(next(x for x in every if x.label == "basic")):]]
        self.assertNotIn("crates.io", basic_hosts)
        self.assertNotIn("pypi.org", basic_hosts)

    def test_forges_not_connected_or_without_a_default(self) -> None:
        with connected():
            forge = self.lines(_proposal())[-1]
        self.assertEqual(("forge", "warn"), (forge.label, forge.tone))
        self.assertIn("forge connect https://gitea.example.com", forge.detail)
        two = replace(GITEA, users=(ForgeUser("a", "A"), ForgeUser("b", "B")))
        with connected(two):
            self.assertIn("none is the default", self.lines(_proposal())[-1].detail)
        deploy = replace(GITEA, users=(ForgeUser("bot", "X", default=True,
                                                 transport="deploy-key"),))
        with connected(deploy):
            self.assertEqual("as bot over deploy-key", self.lines(_proposal())[-1].detail)

    def test_a_broken_connections_file(self) -> None:
        def broken() -> dict[str, object]:
            raise ForgeConnectionError("forges.md: bad")
        with unittest.mock.patch("bot_bottle.forge_connections.connections", new=broken):
            forge = self.lines(_proposal())[-1]
        self.assertEqual(("forges.md: bad", "warn"), (forge.detail, forge.tone))

    def test_a_new_bottle_and_nothing_added(self) -> None:
        p = _proposal(option=OPTION_NEW_EXTENDING, bottles=[], new_bottle=ProposalNewBottle(
            "weather", extends="basic"), additions=ProposalAdditions(
                egress=[ProposalEgress("api.open-meteo.com", ["GET"], "forecasts")]))
        lines = self.lines(p)
        self.assertEqual(["new", "basic"], [x.label for x in lines if x.label])
        plain = self.lines(_proposal(bottles=[]))
        self.assertEqual(["basic"], [x.label for x in plain if x.label])

    def test_a_shadowed_builtin_and_a_long_form_repo(self) -> None:
        (self.bottles / "basic.md").write_text(
            "---\nextends: basic\ngit-gate:\n  repos:\n    tools:\n"
            "      url: ssh://git@example.com/tools.git\n      key:\n        read_only: true\n"
            "---\n")
        lines = self.lines(_proposal(bottles=["basic"]))
        self.assertEqual([("basic", "1 repo", "tools", "read")],
                         [(x.label, x.what, x.detail, x.access) for x in lines[:1]])
        self.assertEqual("basic", lines[1].label)  # the built-in it extends

    def test_nothing_to_read(self) -> None:
        self.assertIsNone(review.review(_proposal(bottles=["nowhere"]), self.dirs))
        self.assertIsNone(review.every_line(_proposal(bottles=["nowhere"]), self.dirs))
        (self.bottles / "empty.md").write_text("---\nextends: basic\n---\n")
        lines = self.lines(_proposal(bottles=["empty"]))
        self.assertEqual(("empty", "nothing beyond its parents"), (lines[0].label, lines[0].what))


class TestGlance(TestReview):
    def test_the_new_grants_and_what_to_do_first(self) -> None:
        add = ProposalAdditions(
            egress=[ProposalEgress("api.linear.app", ["GET"], "read tickets")],
            repos=[ProposalRepo("gitea.example.com/acme/ops:write", "label issues"),
                   ProposalRepo("gitea.example.com/acme/wiki", "read docs")],
            env=[ProposalEnv("MODE", "the mode")],
            credentials=[ProposalCredential("LINEAR_TOKEN", "api.linear.app")])
        with connected():
            seen = review.glance(_proposal(additions=add, summary="Adds Linear."), self.dirs)
        assert seen is not None
        self.assertEqual("Adds Linear.", seen.gist)
        self.assertEqual(["api.linear.app", "acme/ops (push)", "acme/wiki", "MODE",
                          "$LINEAR_TOKEN"], seen.new)
        self.assertEqual({"gitea.example.com": "https://gitea.example.com"}, seen.connect)
        self.assertEqual([], seen.warns)
        with connected(GITEA):
            seen = review.glance(_proposal(), self.dirs)
        assert seen is not None
        self.assertEqual(([], []), (seen.new, seen.warns))
        self.assertIsNone(review.glance(_proposal(bottles=["nowhere"]), self.dirs))

    def test_a_gist_only_names_what_it_reaches(self) -> None:
        hosts = {"api.example.com", "pypi.org"}
        for ok in ["Builds on team. Adds api.example.com so the agent can call it.",
                   "Builds on team. Adds open-meteo so the agent can write forecast.md."]:
            self.assertEqual(ok, review.gist(_proposal(summary=ok), hosts))
        made = "Run in team."
        for bad in ["Adds evil.example.net too.", "x" * 200, ""]:
            with self.subTest(gist=bad):
                self.assertEqual(made, review.gist(_proposal(summary=bad), hosts))


class TestCard(unittest.TestCase):
    def test_columns_brackets_and_wrapping(self) -> None:
        lines = [
            Line("new", "api.linear.app", "read tickets", "GET, POST", "$LINEAR_TOKEN", "new"),
            Line("basic", "15 hosts", "Package registries and public code", "GET"),
            Line("", "pypi.org", "", "any", tone="write"),
            Line("forge", "gitea.example.com", "not connected here; connect it with "
                 "`bot-bottle forge connect https://gitea.example.com`", tone="warn"),
            Line("team", "", "a header"),
        ]
        out = review_card.card(lines, width=60, style=PLAIN)
        self.assertEqual("new    api.linear.app     read tickets (GET, POST)", out[0])
        self.assertEqual(" " * 26 + "with $LINEAR_TOKEN", out[1])
        self.assertTrue(out[2].startswith("basic  15 hosts           Package registries"))
        self.assertTrue(out[2].endswith("(GET)"))
        self.assertEqual("       pypi.org           (any)", out[3])
        self.assertIn("forge connect", "".join(out[4:-1]))
        self.assertTrue(all(len(x) <= 60 for x in out))
        self.assertEqual("team   a header", out[-1])
        fits = review_card.card([Line("new", "h", "why", "GET", "$T", "new")], width=60,
                                style=PLAIN)
        self.assertEqual(["new  h  why (GET) with $T"], fits)

    def test_window(self) -> None:
        rows = [str(i) for i in range(10)]
        self.assertEqual((rows, 0), review_card.window(rows, 5, 20, style=PLAIN))
        shown, offset = review_card.window(rows, 99, 4, style=PLAIN)
        self.assertEqual((["7", "8", "9", "↑ 7 more · ↑↓ scroll"], 7), (shown, offset))
        shown, offset = review_card.window(rows, -3, 4, style=PLAIN)
        self.assertEqual((["0", "1", "2", "↓ 7 more · ↑↓ scroll"], 0), (shown, offset))
        shown, _ = review_card.window(rows, 2, 4, style=PLAIN)
        self.assertEqual("↑ 2 more · ↓ 5 more · ↑↓ scroll", shown[-1])


if __name__ == "__main__":
    unittest.main()
