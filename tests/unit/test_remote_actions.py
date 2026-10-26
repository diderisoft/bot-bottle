"""Connecting a forge from the console over the host link (PRD 0105)."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import remote
from bot_bottle.forge_connect import ConnectError, Connected

DONE = Connected(key="gitea.example.com", bottle="gitea", path=Path("/x/gitea.md"),
                 account="bot", admin_repos=2, repos=5, ssh="ssh://git@h:2222",
                 host_key="ssh-ed25519 AAAA", secret_name="GITEA_EXAMPLE_COM_TOKEN",
                 warnings=("w",))


class TestForgeConnect(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        self.link = remote.ConsoleLink(mock.Mock(), send, agents=lambda: [])

    def handle(self, **msg: Any) -> dict[str, Any]:
        asyncio.run(self.link.handle({"type": "forge_connect", "request_id": "q1", **msg}))
        return self.sent[-1]

    def test_connects_and_reports_without_the_token(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect", return_value=DONE) as connect:
            reply = self.handle(url="gitea.example.com", token="s3cret-token", bottle="work",
                                name="", ssh="ssh://git@h:2222")
        self.assertEqual(("forge_connected", "q1", "gitea.example.com", "bot", 2, 5, ["w"]),
                         (reply["type"], reply["request_id"], reply["key"], reply["account"],
                          reply["admin_repos"], reply["repos"], reply["warnings"]))
        self.assertNotIn("s3cret-token", repr(reply))
        args, kwargs = connect.call_args
        self.assertEqual(("gitea.example.com", "s3cret-token"), args)
        self.assertEqual(("work", "ssh://git@h:2222"), (kwargs["bottle"], kwargs["ssh"]))

    def test_says_why_it_failed(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect",
                        side_effect=ConnectError("the token didn't work: HTTP 401")):
            self.assertEqual("the token didn't work: HTTP 401",
                             self.handle(url="h", token="t")["error"])
        with mock.patch("bot_bottle.forge_connect.connect", side_effect=OSError("disk")):
            self.assertIn("see the host controller's log", self.handle(url="h", token="t")["error"])

    def test_not_into_a_project_bottle(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect") as connect:
            self.assertIn("host bottles", self.handle(url="h", token="t", bottle="./main")["error"])
        connect.assert_not_called()

    def test_bad_field_types_fall_back(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect", return_value=DONE) as connect:
            self.handle(url="h", token="t", bottle=5)
        self.assertEqual("gitea", connect.call_args.kwargs["bottle"])


if __name__ == "__main__":
    unittest.main()
