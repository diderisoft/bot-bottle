"""Unit: `create-repo` (PRD 0120): a bottle's `git_gate.actions.create`, the
forge API calls, and the host creating and granting the repo, with a card or
without one."""

from __future__ import annotations

import asyncio
import io
import json
import unittest
import urllib.error
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.contrib.gitea import forge_api as gitea_api
from bot_bottle.contrib.github import forge_api as github_api
from bot_bottle.control.hostctl import remote_actions, repo_create
from bot_bottle.forge_kinds import ForgeApiError
from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.extends import merge_bottles_runtime
from bot_bottle.manifest.forges import ManifestForge
from bot_bottle.manifest.git import create_allowed
from bot_bottle.manifest.trust import grants
from bot_bottle.manifest.util import ManifestError

FORGE = ManifestForge(name="github.com", kind="github", url="https://github.com",
                      ssh="ssh://git@github.com", token_secret="GH", account="octo")
APP_FORGE = ManifestForge(name="github.com", kind="github", url="https://github.com",
                          ssh="ssh://git@github.com", token_secret="GH", account="octo",
                          app="bot-bottle-dev")


def _bottle(*create: str) -> ManifestBottle:
    return ManifestBottle.from_dict("b", {"git_gate": {"actions": {"create": list(create)}}})


class TestActions(unittest.TestCase):
    def test_parsed_merged_shown_and_written(self) -> None:
        a, b = _bottle("github.com/octo/*"), _bottle("gitea.example.com/acme/tool",
                                                      "github.com/octo/*")
        merged = merge_bottles_runtime([a, b])
        self.assertEqual(("github.com/octo/*", "gitea.example.com/acme/tool"), merged.git_create)
        self.assertIn("create  repos at github.com/octo/*", grants(merged))
        self.assertEqual({"create": ["github.com/octo/*", "gitea.example.com/acme/tool"]},
                         bottle_document(merged)["git_gate"]["actions"])  # type: ignore[index]

    def test_bad_entries(self) -> None:
        for bad in ({"create": ["github.com/*/*"]}, {"create": ["octo/*"]},
                    {"create": "github.com/octo/*"}, {"delete": []}):
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                ManifestBottle.from_dict("b", {"git_gate": {"actions": bad}})

    def test_create_allowed(self) -> None:
        patterns = ("github.com/octo/*", "gitea.example.com/acme/tool")
        self.assertTrue(create_allowed(patterns, "github.com/octo/anything"))
        self.assertTrue(create_allowed(patterns, "gitea.example.com/acme/tool"))
        self.assertFalse(create_allowed(patterns, "gitea.example.com/acme/other"))
        self.assertFalse(create_allowed(patterns, "github.com/someone/x"))


def _http_error(code: int, body: dict[str, object]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", {},  # type: ignore[arg-type]
                                  io.BytesIO(json.dumps(body).encode()))


class _Resp(io.BytesIO):
    status = 201

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class TestForgeApi(unittest.TestCase):
    def test_github_creates_under_the_user_or_an_org(self) -> None:
        sent: list[Any] = []

        def urlopen(req: Any, timeout: float) -> _Resp:
            del timeout
            sent.append(req)
            return _Resp(json.dumps({"html_url": "https://github.com/octo/new"}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            url = github_api.create_repo("https://github.com", "t", owner="octo", name="new",
                                         private=True, as_user=True)
            github_api.create_repo("https://github.com", "t", owner="acme", name="new",
                                   private=False, as_user=False)
        self.assertEqual("https://github.com/octo/new", url)
        self.assertEqual(["https://api.github.com/user/repos",
                          "https://api.github.com/orgs/acme/repos"], [r.full_url for r in sent])
        self.assertEqual({"name": "new", "private": True, "auto_init": False},
                         json.loads(sent[0].data))
        self.assertEqual("Bearer t", sent[0].headers["Authorization"])

    def test_a_refusal_says_why(self) -> None:
        with mock.patch("urllib.request.urlopen",
                        side_effect=_http_error(403, {"message": "Resource not accessible"})):
            with self.assertRaises(ForgeApiError) as cm:
                gitea_api.create_repo("https://g.example", "t", owner="o", name="n",
                                      private=True, as_user=True)
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertIn("Resource not accessible", str(cm.exception))

    def test_exists(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404, {})):
            self.assertFalse(gitea_api.repo_exists("https://g.example", "t", "o", "n"))
        with mock.patch("urllib.request.urlopen", return_value=_Resp(b"{}")):
            self.assertTrue(github_api.repo_exists("https://github.com", "t", "o", "n"))
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500, {})), \
                self.assertRaises(ForgeApiError):
            github_api.repo_exists("https://github.com", "t", "o", "n")


class TestPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.forge = self.enterContext(mock.patch(
            "bot_bottle.control.hostctl.repo_grants.find_forge", return_value=FORGE))
        self.enterContext(mock.patch.object(repo_create, "_token", return_value="t"))
        self.enterContext(mock.patch(
            "bot_bottle.control.hostctl.repo_grants.proposed_alias", return_value=""))
        self.api = mock.Mock()
        self.api.repo_exists.return_value = False
        self.api.create_repo.return_value = "https://github.com/octo/new"
        self.enterContext(mock.patch("bot_bottle.forge_kinds.forge_api", return_value=self.api))

    def test_parse(self) -> None:
        self.assertEqual(repo_create.NewRepo("github.com", "octo", "new", private=False),
                         repo_create.parse("github.com/octo/new:public"))
        self.assertTrue(repo_create.parse("github.com/octo/new").private)
        with self.assertRaises(repo_create.CreateRepoError):
            repo_create.parse("octo/new")

    def test_plan_refuses_an_unconnected_forge_or_an_existing_repo(self) -> None:
        new, forge = repo_create.plan("s1", "github.com/octo/new")
        self.assertEqual(("github.com/octo/new", FORGE), (new.repo, forge))
        self.api.repo_exists.return_value = True
        with self.assertRaisesRegex(repo_create.CreateRepoError, "exists already.*add-repo"):
            repo_create.plan("s1", "github.com/octo/new")
        self.forge.return_value = None
        with self.assertRaisesRegex(repo_create.CreateRepoError, "forge-connect"):
            repo_create.plan("s1", "github.com/octo/new")

    def test_through_the_app_it_must_be_installed_with_administration(self) -> None:
        self.forge.return_value = APP_FORGE
        found: list[dict[str, str]] = []

        def installations(_token: str) -> list[dict[str, str]]:
            return found

        self.enterContext(mock.patch(
            "bot_bottle.contrib.github.user_token.installations", side_effect=installations))
        with self.assertRaisesRegex(repo_create.CreateRepoError,
                                    "isn't installed on octo.*apps/bot-bottle-dev/installations"):
            repo_create.plan("s1", "github.com/octo/new")
        found.append({"account": "Octo", "type": "User", "administration": ""})
        with self.assertRaisesRegex(repo_create.CreateRepoError,
                                    "Administration.*github.com/settings/installations"):
            repo_create.plan("s1", "github.com/octo/new")
        found.append({"account": "acme", "type": "Organization", "administration": "read"})
        with self.assertRaisesRegex(repo_create.CreateRepoError,
                                    "organizations/acme/settings/installations"):
            repo_create.plan("s1", "github.com/acme/new")
        found[0]["administration"] = "write"
        new, _ = repo_create.plan("s1", "github.com/octo/new")
        self.assertEqual("github.com/octo/new", new.repo)
        self.assertEqual([], repo_create.not_ready_lines("s1", "github.com/octo/new"))
        self.assertEqual("install: https://github.com/organizations/acme/settings/installations",
                         repo_create.not_ready_lines("s1", "github.com/acme/new")[0])

    def test_create_and_grant(self) -> None:
        new = repo_create.parse("github.com/octo/new")
        grant = mock.Mock(return_value={"url": "https://github.com/octo/new.git"})
        note = repo_create.create_and_grant("s1", new, FORGE, grant)
        grant.assert_called_once_with("s1", "github.com/octo/new:write")
        self.assertEqual(True, self.api.create_repo.call_args.kwargs["as_user"])
        self.assertIn("created github.com/octo/new (private", note)
        self.assertIn("push to its normal URL: https://github.com/octo/new.git", note)
        grant.return_value = {"error": "gate down"}
        with self.assertRaisesRegex(repo_create.CreateRepoError,
                                    "granting it failed: gate down; ask for it with add-repo"):
            repo_create.create_and_grant("s1", new, FORGE, grant)


def _request(proposed: str = "github.com/octo/new") -> SimpleNamespace:
    return SimpleNamespace(id="p1", tool="create-repo", proposed=proposed, session="s1",
                           bottle="b1", orchestrator="http://orch", justification="for the port")


class TestRemote(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.orchestrator = mock.Mock(return_value=self.client)
        self.bottles = mock.Mock()
        self.bottles.grant_repo.return_value = {"url": "https://github.com/octo/new.git"}
        self.enterContext(mock.patch("bot_bottle.control.hostctl.nudge.nudge_if_waited"))
        self.plan = self.enterContext(mock.patch.object(
            repo_create, "plan", return_value=(repo_create.parse("github.com/octo/new"), FORGE)))
        self.allowed = self.enterContext(mock.patch.object(
            repo_create, "allowed_by_bottle", return_value=False))
        self.create = self.enterContext(mock.patch.object(
            repo_create, "create", return_value="https://github.com/octo/new"))
        self.alias = self.enterContext(mock.patch(
            "bot_bottle.control.hostctl.repo_grants.proposed_alias", return_value=""))

    def card(self) -> object:
        return asyncio.run(remote_actions.special_card(_request(), self.orchestrator,
                                                       self.bottles))

    def test_a_card_says_what_happens(self) -> None:
        lines, options = self.card()  # type: ignore[misc]
        self.assertEqual(["create_repo"], options)
        self.assertEqual(["create: github.com/octo/new (private)", "as: octo on github.com",
                          "then: granted to this bottle with write", "why: for the port"],
                         lines)
        self.create.assert_not_called()

    def test_a_taken_name_is_granted_under_an_alias(self) -> None:
        self.alias.return_value = "new-github"
        lines, _ = self.card()  # type: ignore[misc]
        self.assertIn("then: granted to this bottle with write, as new-github", lines[2])
        message = asyncio.run(remote_actions.answer(self.client, _request(), "allow_once",
                                                    self.bottles, via="the phone"))
        self.bottles.grant_repo.assert_called_once_with(
            "s1", "new-github=github.com/octo/new:write")
        self.assertIn("(as new-github in the gate)", message)

    def test_allowing_it_creates_and_grants(self) -> None:
        message = asyncio.run(remote_actions.answer(self.client, _request(), "allow_once",
                                                    self.bottles, via="the phone"))
        self.assertIn("created github.com/octo/new", message)
        self.bottles.grant_repo.assert_called_once_with("s1", "github.com/octo/new:write")
        kw = self.client.supervise_respond.call_args.kwargs
        self.assertEqual("approve", kw["decision"])

    def test_the_bottle_allows_it_without_a_card(self) -> None:
        self.allowed.return_value = True
        self.assertEqual(([], []), self.card())
        self.create.assert_called_once()
        self.assertIn("from the bottle (actions.create)",
                      self.client.supervise_respond.call_args.kwargs["notes"])

    def test_one_that_cant_be_made_is_refused_unseen(self) -> None:
        self.plan.side_effect = repo_create.CreateRepoError("github.com/octo/new exists already")
        self.assertEqual(([], []), self.card())
        kw = self.client.supervise_respond.call_args.kwargs
        self.assertEqual("reject", kw["decision"])
        self.assertIn("exists already", kw["notes"])

    def test_until_the_app_is_ready_the_card_links_the_fix_and_waits(self) -> None:
        url = "https://github.com/apps/bot-bottle-dev/installations/new"
        self.plan.side_effect = repo_create.AppNotReady("for the agent", url, "not installed: x")
        remote_actions._unreached.clear()  # pylint: disable=protected-access
        self.addCleanup(remote_actions._unreached.clear)  # pylint: disable=protected-access
        self.enterContext(mock.patch.object(remote_actions.time, "monotonic", return_value=100.0))
        lines, options = self.card()  # type: ignore[misc]
        self.assertEqual(["create: github.com/octo/new", f"install: {url}", "not installed: x",
                          "why: for the port"], lines)
        self.assertEqual(["create_repo"], options)
        with self.assertRaisesRegex(ValueError, "still waiting"):  # allowed too soon
            asyncio.run(remote_actions.answer(self.client, _request(), "allow_once",
                                              self.bottles, via="the phone"))
        self.client.supervise_respond.assert_not_called()

        def changed(at: float) -> bool:
            return asyncio.run(remote_actions.card_changed(_request(), now=at))

        with mock.patch.object(repo_create, "not_ready_lines",
                               side_effect=[[f"install: {url}", "not installed: x"], []]):
            self.assertFalse(changed(111.0))  # still not installed
            self.assertTrue(changed(122.0))   # installed: the card is raised again
        self.plan.side_effect = None
        lines, _ = self.card()  # type: ignore[misc]
        self.assertEqual("create: github.com/octo/new (private)", lines[0])

    def test_the_consoles_create_says_what_happened(self) -> None:
        request = _request()
        request.arrived = "2026-11-07T08:00:00+00:00"
        asking = {"a1": request}

        def create(since: str = request.arrived) -> dict[str, object]:
            return asyncio.run(remote_actions.ANSWERS["create_repo_answer"](
                {"agent_id": "a1", "since": since, "request_id": "r1"}, asking,
                self.orchestrator, self.bottles))

        self.assertEqual("That request is no longer waiting.",
                         create("2026-11-07T09:00:00+00:00")["error"])
        self.plan.side_effect = repo_create.AppNotReady("x", "https://u", "not installed: x")
        self.assertIn("still waiting", str(create()["error"]))
        self.client.supervise_respond.assert_not_called()
        self.plan.side_effect = None
        done = create()
        self.assertEqual("create_repo_answered", done["type"])
        self.assertIn("created github.com/octo/new", str(done["message"]))
        self.assertNotIn("refused", done)
        self.plan.side_effect = repo_create.CreateRepoError("it exists already")
        self.assertTrue(create()["refused"])

    def test_deny_is_plain(self) -> None:
        asyncio.run(remote_actions.answer(self.client, _request(), "deny", self.bottles,
                                          via="the phone"))
        self.assertEqual("reject", self.client.supervise_respond.call_args.kwargs["decision"])
        self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
