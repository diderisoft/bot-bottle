"""Unit: the host's forge connections for the console (ADR 0021): status and
repo lists with permissions, and never a token."""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest import mock

from bot_bottle.contrib.github.user_token import ReauthorizeError
from bot_bottle.control.hostctl import forge_link
from bot_bottle.forge_connections import ForgeConnection, ForgeUser
from bot_bottle.forge_kinds import ForgeApiError

TOKEN = "ghu_SECRET_VALUE"
GITHUB = ForgeConnection(host="github.com", kind="github", url="https://github.com",
                         ssh="ssh://git@github.com", users=(
                             ForgeUser(name="octo", token_secret="GH_OCTO", default=True,
                                       app="bot-bottle"),))
GITEA = ForgeConnection(host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
                        ssh="ssh://git@gitea.example.com:2222", users=(
                            ForgeUser(name="me", token_secret="GITEA_ME", default=True),))


def _repo(name: str, push: bool, private: bool = True) -> dict[str, Any]:
    return {"full_name": name, "private": private,
            "permissions": {"pull": True, "push": push, "admin": False}}


class _Forge:
    """Answers forge API URLs by prefix; records what was asked."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def __call__(self, kind: str, url: str, token: str) -> object:
        self.asked.append(url)
        assert token == TOKEN
        for prefix, body in self.routes.items():
            if prefix in url:
                return body(url) if callable(body) else body
        return []


class Status(unittest.TestCase):
    def _status(self, conns: dict[str, ForgeConnection], token: str | None = TOKEN,
                installations: "list[dict[str, str]] | Exception | None" = None
                ) -> list[dict[str, Any]]:
        target = "bot_bottle.contrib.github.user_token.installations"
        inst = (mock.patch(target, side_effect=installations)
                if isinstance(installations, Exception)
                else mock.patch(target, return_value=list(installations or [])))
        with mock.patch("bot_bottle.forge_connections.connections", return_value=conns), \
                mock.patch.object(forge_link, "_token", return_value=token), inst:
            return forge_link.status()

    def test_each_connection_and_user_without_a_token(self) -> None:
        found = self._status({"github.com": GITHUB, "gitea.example.com": GITEA},
                             installations=[{"account": "octo", "repository_selection": "all"}])
        self.assertEqual(["gitea.example.com", "github.com"], [f["host"] for f in found])
        gh = found[1]
        self.assertEqual("https://github.com", gh["url"])
        user = gh["users"][0]
        self.assertEqual(("octo", True, "bot-bottle", "ok"),
                         (user["login"], user["default"], user["app"], user["state"]))
        self.assertEqual([{"account": "octo", "repository_selection": "all"}],
                         user["installations"])
        self.assertNotIn(TOKEN, json.dumps(found))
        self.assertNotIn("GH_OCTO", json.dumps(found))  # nor the secret's name

    def test_states(self) -> None:
        self.assertEqual("missing", self._status({"g": GITEA}, token=None)[0]["users"][0]["state"])
        self.assertEqual("needs_install",
                         self._status({"g": GITHUB}, installations=[])[0]["users"][0]["state"])
        expired = self._status({"g": GITHUB}, installations=ReauthorizeError("re-authorize"))
        self.assertEqual(("expired", "re-authorize"),
                         (expired[0]["users"][0]["state"], expired[0]["users"][0]["message"]))
        broken = self._status({"g": GITHUB}, installations=ForgeApiError("HTTP 500"))
        self.assertEqual("error", broken[0]["users"][0]["state"])


class Repos(unittest.TestCase):
    def _repos(self, conn: ForgeConnection, forge: _Forge, login: str = "") -> dict[str, Any]:
        with mock.patch("bot_bottle.forge_connections.get", return_value=conn), \
                mock.patch.object(forge_link, "_token", return_value=TOKEN), \
                mock.patch.object(forge_link, "_get", side_effect=forge):
            return forge_link.repos(conn.host, login)

    def test_github_app_marks_what_the_app_reaches(self) -> None:
        forge = _Forge({
            "/user/installations?": {"installations": [{"id": 7}]},
            "/user/installations/7/repositories": {"repositories": [
                _repo("octo/app", True), _repo("acme/lib", False)]},
            "/user/repos": [_repo("octo/app", True), _repo("other/thing", True, private=False)],
        })
        got = self._repos(GITHUB, forge)
        self.assertEqual(("github.com", "octo", False), (got["forge"], got["login"],
                                                         got["truncated"]))
        by = {r["full_name"]: r for r in got["repos"]}
        self.assertEqual(["acme/lib", "octo/app", "other/thing"], list(by))  # sorted
        self.assertTrue(by["octo/app"]["installed"])
        self.assertFalse(by["acme/lib"]["permissions"]["push"])  # read-only: no Write
        self.assertIs(False, by["other/thing"]["installed"])  # reachable, App not installed
        self.assertNotIn(TOKEN, json.dumps(got))

    def test_gitea_pages_until_a_short_page(self) -> None:
        full = [_repo(f"me/r{i}", True) for i in range(forge_link._GITEA_PAGE)]  # pylint: disable=protected-access
        forge = _Forge({"page=1": full, "page=2": [_repo("me/last", False)]})
        got = self._repos(GITEA, forge)
        self.assertEqual(forge_link._GITEA_PAGE + 1, len(got["repos"]))  # pylint: disable=protected-access
        self.assertTrue(all(r["installed"] is None for r in got["repos"]))
        self.assertTrue(forge.asked[0].startswith("https://gitea.example.com/api/v1/user/repos"))

    def test_a_cap_marks_the_list_truncated(self) -> None:
        page = [_repo(f"me/r{i}", True) for i in range(forge_link._GITEA_PAGE)]  # pylint: disable=protected-access
        with mock.patch.object(forge_link, "MAX_REPOS", 120):
            got = self._repos(GITEA, _Forge({"user/repos": page}))
        self.assertEqual((120, True), (len(got["repos"]), got["truncated"]))

    def test_errors_say_why(self) -> None:
        with mock.patch("bot_bottle.forge_connections.get", return_value=None), \
                self.assertRaisesRegex(forge_link.ForgeLinkError, "isn't connected"):
            forge_link.repos("nope.example.com")
        with self.assertRaisesRegex(forge_link.ForgeLinkError, "isn't connected on"):
            self._repos(GITEA, _Forge({}), login="someone-else")
        with mock.patch("bot_bottle.forge_connections.get", return_value=GITEA), \
                mock.patch.object(forge_link, "_token", return_value=None), \
                self.assertRaisesRegex(forge_link.ForgeLinkError, "no token"):
            forge_link.repos(GITEA.host)

        def boom(*_a: object) -> object:
            raise ForgeApiError("HTTP 502")
        with self.assertRaisesRegex(forge_link.ForgeLinkError, "502"):
            self._repos(GITEA, boom)  # type: ignore[arg-type]


class Messages(unittest.TestCase):
    def test_replies_carry_the_request_id_and_errors(self) -> None:
        with mock.patch.object(forge_link, "status", return_value=[]):
            reply = asyncio.run(forge_link.forge_status({"request_id": "r1"}))
        self.assertEqual({"type": "forge_status", "request_id": "r1", "forges": []}, reply)
        self.assertEqual("which forge?",
                         asyncio.run(forge_link.forge_repos({"request_id": "r2"}))["error"])
        with mock.patch.object(forge_link, "repos",
                               side_effect=forge_link.ForgeLinkError("not connected")):
            reply = asyncio.run(forge_link.forge_repos({"request_id": "r3", "forge": "x"}))
        self.assertEqual(("forge_repos", "not connected"), (reply["type"], reply["error"]))
        with mock.patch.object(forge_link, "repos", side_effect=ValueError(TOKEN)):
            reply = asyncio.run(forge_link.forge_repos({"request_id": "r4", "forge": "x"}))
        self.assertNotIn(TOKEN, reply["error"])  # an unexpected error's text isn't sent
        with mock.patch.object(forge_link, "status", side_effect=forge_link.ForgeLinkError("bad")):
            self.assertEqual("bad", asyncio.run(forge_link.forge_status({}))["error"])
        with mock.patch.object(forge_link, "status", side_effect=OSError(TOKEN)):
            self.assertNotIn(TOKEN, asyncio.run(forge_link.forge_status({}))["error"])

    def test_the_link_routes_them(self) -> None:
        from bot_bottle.control.hostctl.remote_actions import HOST_REQUESTS
        for kind in ("forge_status", "forge_repos", "github_status"):
            self.assertIn(kind, HOST_REQUESTS)


if __name__ == "__main__":
    unittest.main()
