"""Connecting a forge from the console over the host link (PRD 0105)."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.approvals import Request
from bot_bottle.control.client import AlreadyAnswered
from bot_bottle.control.hostctl import remote, remote_actions
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


ARRIVED = "2026-10-26T10:00:00+00:00"


def _ask(tool: str = "forge-connect") -> Request:
    return Request(id="p1", bottle="b1", session="s", tool=tool, justification="need the repo",
                   proposed="https://gitea.example.com", current="", arrived=ARRIVED,
                   orchestrator="http://orch")


class TestForgeAttention(unittest.TestCase):
    def test_marks_a_forge_new_to_this_host(self) -> None:
        with mock.patch.object(remote_actions, "_connected_hosts", return_value=set()):
            lines, options = remote_actions.forge_attention(_ask())
        self.assertEqual(["forge_connect"], options)
        text = "\n".join(lines)
        for part in ("forge: https://gitea.example.com", "why: need the repo", "new to this host",
                     "https://gitea.example.com/user/settings/applications", "write:repository"):
            self.assertIn(part, text)
        with mock.patch.object(remote_actions, "_connected_hosts",
                               return_value={"gitea.example.com"}):
            lines, _ = remote_actions.forge_attention(_ask())
        self.assertNotIn("new to this host", "\n".join(lines))

    def test_connected_hosts_reads_host_bottles(self) -> None:
        import tempfile  # pylint: disable=import-outside-toplevel
        with tempfile.TemporaryDirectory() as root:
            bottles = Path(root) / "bottles"
            bottles.mkdir()
            (bottles / "gitea.md").write_text(
                "---\ngit-gate:\n  forges:\n    a:\n      url: https://gitea.example.com\n---\n")
            (bottles / "bad.md").write_text("---\n: [\n---\n")
            with mock.patch("bot_bottle.paths.bot_bottle_root", return_value=Path(root)):
                self.assertEqual({"gitea.example.com"}, remote_actions._connected_hosts())


class TestForgeConnectAnswer(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.asking: dict[str, Any] = {"a1": _ask()}

    def answer(self, **msg: Any) -> dict[str, Any]:
        return asyncio.run(remote_actions.forge_connect_answer(
            {"request_id": "q2", "agent_id": "a1", "since": ARRIVED, "token": "s3cret", **msg},
            self.asking, self.orchestrator))

    def orchestrator(self, _url: str) -> Any:
        return self.client

    def test_connects_the_agents_url_and_approves(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect", return_value=DONE) as connect:
            reply = self.answer(url="https://evil.example.com", bottle="work")
        self.assertEqual(("forge_connect_answered", "q2", "bot"),
                         (reply["type"], reply["request_id"], reply["account"]))
        self.assertNotIn("s3cret", repr(reply))
        self.assertEqual("https://gitea.example.com", connect.call_args.args[0])
        args, kwargs = self.client.supervise_respond.call_args
        self.assertEqual(("p1", "b1", "approve"), (args[0], kwargs["bottle_slug"],
                                                    kwargs["decision"]))
        self.assertIn("as bot", kwargs["notes"])
        self.assertNotIn("s3cret", kwargs["notes"])

    def test_failed_connect_leaves_it_waiting(self) -> None:
        with mock.patch("bot_bottle.forge_connect.connect",
                        side_effect=ConnectError("the token didn't work")):
            self.assertEqual("the token didn't work", self.answer()["error"])
        self.client.supervise_respond.assert_not_called()

    def test_only_a_waiting_forge_request(self) -> None:
        self.assertIn("no longer waiting", self.answer(since="2026-10-26T11:00:00+00:00")["error"])
        self.asking["a1"] = _ask("egress-allow")
        self.assertIn("no longer waiting", self.answer()["error"])
        self.asking.clear()
        self.assertIn("no longer waiting", self.answer()["error"])

    def test_answered_elsewhere_is_a_warning(self) -> None:
        self.client.supervise_respond.side_effect = AlreadyAnswered("approved")
        with mock.patch("bot_bottle.forge_connect.connect", return_value=DONE):
            reply = self.answer()
        self.assertIn("answered elsewhere", reply["warnings"][-1])
        self.assertNotIn("error", reply)


if __name__ == "__main__":
    unittest.main()
