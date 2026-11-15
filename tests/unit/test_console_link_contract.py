"""Unit: the console link's protocol (contract/console_link.py, ADR 0024)."""

from __future__ import annotations

import ast
import asyncio
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract import console_link
from bot_bottle.control.hostctl import link_protocol, remote, remote_actions


def _link() -> tuple[remote.ConsoleLink, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    return remote.ConsoleLink(object(), send, agents=lambda: []), sent  # type: ignore[arg-type]


class TestContract(unittest.TestCase):
    def test_the_host_handles_exactly_the_contracts_messages(self) -> None:
        self.assertEqual(console_link.CONSOLE_MESSAGES, remote.ConsoleLink.handles())

    def test_frozen_messages_and_card_answers_are_console_messages(self) -> None:
        self.assertLessEqual(console_link.FROZEN, console_link.CONSOLE_MESSAGES)
        self.assertLessEqual(set(console_link.CARD_ANSWERS.values()),
                             console_link.CONSOLE_MESSAGES)

    def test_each_card_answer_is_one_the_host_answers_cards_with(self) -> None:
        self.assertEqual(set(console_link.CARD_ANSWERS.values()), set(remote_actions.ANSWERS))

    def test_the_contract_imports_nothing_of_bot_bottles(self) -> None:
        # The console vendors it verbatim.
        tree = ast.parse(Path(console_link.__file__).read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual({"__future__"}, modules)


class TestHostEnd(unittest.TestCase):
    def test_hello_says_the_protocol_and_version(self) -> None:
        link, sent = _link()
        with mock.patch("bot_bottle.engine.install_version", return_value=("0.1.18", "abc")):
            asyncio.run(link.hello())
        self.assertEqual([{"type": "hello", "protocol": console_link.LINK_PROTOCOL,
                           "version": "0.1.18"}], sent)

    def test_ping_is_answered(self) -> None:
        link, sent = _link()
        asyncio.run(link.handle({"type": "ping", "request_id": "p1"}))
        self.assertEqual([{"type": "pong", "request_id": "p1"}], sent)

    def test_an_unknown_message_is_answered_unsupported(self) -> None:
        link, sent = _link()
        with mock.patch.object(link_protocol.log, "warn") as warn:
            asyncio.run(link.handle({"type": "teleport", "request_id": "r1"}))
        self.assertEqual("unsupported", sent[0]["type"])
        self.assertEqual(("r1", "teleport"), (sent[0]["request_id"], sent[0]["message_type"]))
        self.assertIn("upgrade bot-bottle", sent[0]["error"])
        warn.assert_called_once()

    def test_a_console_hello_is_quiet(self) -> None:
        link, sent = _link()
        p = console_link.LINK_PROTOCOL
        with mock.patch.object(link_protocol.log, "warn") as warn:
            for hello in ({"type": "hello"}, {"type": "hello", "protocols": [p, p]},
                          {"type": "hello", "protocols": "junk"}):
                asyncio.run(link.handle(hello))
        self.assertEqual([], sent)
        warn.assert_not_called()

    def test_a_console_of_another_protocol_is_logged(self) -> None:
        link, _sent = _link()
        p = console_link.LINK_PROTOCOL
        with mock.patch.object(link_protocol.log, "warn") as warn:
            asyncio.run(link.handle({"type": "hello", "protocols": [p + 1, p + 2]}))
            asyncio.run(link.handle({"type": "hello", "protocols": [0, p - 1]}))
        self.assertIn("upgrade bot-bottle", warn.call_args_list[0].args[0])
        self.assertIn("console needs updating", warn.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
