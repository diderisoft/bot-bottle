"""Unit: the console link presents sessions it didn't launch (PRD 0100, #717)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from bot_bottle.control.hostctl import remote
from tests.unit.test_remote_link import _Bottles, _pending, _Terminal


class TestTerminalSessions(unittest.TestCase):
    """Sessions the console didn't launch are presented and then work like its
    own (#717)."""

    def setUp(self) -> None:
        self.bottles = _Bottles()
        self.sent: list[dict[str, Any]] = []
        self.terms: dict[str, _Terminal] = {}
        self.state = remote.LinkState()

    def link(self) -> remote.ConsoleLink:
        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        async def terminals(agent_id: str, bottle: str) -> _Terminal:
            del bottle
            self.terms[agent_id] = _Terminal(True)
            return self.terms[agent_id]

        return remote.ConsoleLink(self.bottles, send, agents=lambda: ["implementer"],
                                  terminals=terminals, state=self.state)

    def presented(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == "agent_present"]

    def test_a_terminal_session_is_presented_once_with_a_stable_id(self) -> None:
        self.bottles.start_elsewhere("app")
        link = self.link()
        asyncio.run(link.discover())
        asyncio.run(link.discover())  # already shown: not presented again
        agent_id = remote.terminal_agent_id("app", "act-t")
        self.assertEqual([{"type": "agent_present", "agent_id": agent_id, "bottle": "app",
                           "label": "app", "definition": "implementer", "origin": "terminal",
                           "writable": True, "transcript": False, "activation": "act-t"}],
                         self.presented())
        self.assertEqual({agent_id: "app"}, link.running)
        self.assertIn(agent_id, self.terms)  # its terminal is relayed
        self.assertTrue(agent_id.startswith("t-"))
        self.assertNotEqual(agent_id, remote.terminal_agent_id("app", "act-2"))

    def test_console_launches_and_stopped_sessions_are_not_presented(self) -> None:
        self.bottles.start_elsewhere("from-console", origin="console")
        self.bottles.start_elsewhere("gone")
        self.bottles.running.discard("gone")
        link = self.link()
        asyncio.run(link.handle({"type": "launch", "agent_id": "a1",
                                 "definition": {"name": "implementer"}}))
        self.bottles.listed.append({"id": "bottle-1", "origin": "terminal"})
        asyncio.run(link.discover())
        self.assertEqual([], self.presented())

    def test_a_restarted_host_takes_up_its_console_launches_again(self) -> None:
        # The link's memory went with the old host controller; the session
        # record keeps the console's id, so the agent keeps its relay and id.
        self.bottles.start_elsewhere("from-console", origin="console")
        self.bottles.listed[-1]["agent_id"] = "c-1"
        self.bottles.start_elsewhere("unknown", origin="console")  # recorded before ids were
        link = self.link()
        asyncio.run(link.resume())
        self.assertEqual({"c-1": "from-console"}, link.running)
        self.assertIn("c-1", self.terms)
        self.assertEqual([], self.presented())  # the console has it already
        self.assertIn({"type": "agents_running", "agent_ids": ["c-1"]}, self.sent)

    def test_a_console_bottle_taken_up_is_announced_as_started(self) -> None:
        # `bot-bottle resume` from the CLI brings a console bottle back under
        # its console id: the console flips its stopped entry to running. (A
        # restart announces it too; the console skips a run it already has.)
        self.bottles.start_elsewhere("from-console", origin="console")
        self.bottles.listed[-1]["agent_id"] = "c-3"
        link = self.link()
        asyncio.run(link.discover())
        asyncio.run(link.discover())  # once: it's the link's now
        started = [m for m in self.sent if m["type"] == "agent_started"]
        self.assertEqual(1, len(started))
        self.assertEqual({"agent_id": "c-3", "bottle": "from-console", "writable": True},
                         {k: started[0][k] for k in ("agent_id", "bottle", "writable")})

    def test_a_launch_in_flight_is_not_taken_up(self) -> None:
        self.bottles.start_elsewhere("mid", origin="console")
        self.bottles.listed[-1]["agent_id"] = "c-2"
        link = self.link()
        link._starting.add("c-2")  # pylint: disable=protected-access
        asyncio.run(link.discover())
        self.assertEqual({}, link.running)

    def test_a_reconnect_presents_it_again_under_the_same_id(self) -> None:
        self.bottles.start_elsewhere("app")
        first = self.link()
        asyncio.run(first.resume())
        first.close()
        self.sent.clear()
        asyncio.run(self.link().resume())
        agent_id = remote.terminal_agent_id("app", "act-t")
        self.assertEqual([agent_id], [m["agent_id"] for m in self.presented()])
        self.assertIn({"type": "agents_running", "agent_ids": [agent_id]}, self.sent)

    def test_its_requests_are_raised_and_it_can_be_stopped(self) -> None:
        self.bottles.start_elsewhere("app")
        link = self.link()
        asyncio.run(link.discover())
        agent_id = remote.terminal_agent_id("app", "act-t")
        link._requests = lambda: [_pending(session="app")]  # pylint: disable=protected-access
        asyncio.run(link.push_attention())
        self.assertEqual(agent_id, self.sent[-1]["agent_id"])
        self.assertEqual("attention", self.sent[-1]["type"])
        stopping: list[set[str]] = []
        teardown = self.bottles.teardown

        def watched_teardown(bottle_id: str, role: str = "cli") -> dict[str, object]:
            # Still listed while it's torn down, but not presented again.
            stopping.append(set(link._stopping))  # pylint: disable=protected-access
            return teardown(bottle_id, role)

        self.bottles.teardown = watched_teardown  # type: ignore[method-assign]
        asyncio.run(link.handle({"type": "stop", "agent_id": agent_id}))
        self.assertEqual(["app"], self.bottles.torn_down)
        self.assertEqual([{"app"}], stopping)
        self.assertEqual({"type": "agent_stopped", "agent_id": agent_id, "exit_code": 0},
                         self.sent[-1])
        self.assertEqual({}, link.present)

    def test_an_exited_session_is_reported_stopped(self) -> None:
        self.bottles.start_elsewhere("app")
        link = self.link()
        asyncio.run(link.discover())
        self.bottles.running.discard("app")
        asyncio.run(link.reap())
        self.assertEqual("agent_stopped", self.sent[-1]["type"])
        self.assertEqual({}, link.present)

    def test_a_listing_failure_is_retried_later(self) -> None:
        def broken() -> list[dict[str, object]]:
            raise OSError("hostctl busy")

        self.bottles.list = broken  # type: ignore[method-assign]
        asyncio.run(self.link().discover())
        self.assertEqual([], self.sent)


if __name__ == "__main__":
    unittest.main()
