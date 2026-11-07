"""Unit: the console link's canary events — `canary_fired` once per event for
running agents, and `resolve_canary` passed to the orchestrator."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from bot_bottle.control.hostctl.canary_link import CanaryLink


def _event(event_id: str, slug: str = "slug-1") -> dict[str, Any]:
    return {"id": event_id, "bottle_slug": slug, "reason": "canary token",
            "request_path": "/x", "arrival_timestamp": "2026-11-07T00:00:00+00:00"}


class _Orchestrator:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.resolved: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def canary_events(self) -> list[Any]:
        if self.fail:
            raise RuntimeError("down")
        return self.events

    def canary_resolve(self, event_id: str, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.resolved.append((event_id, kw))


class TestCanaryLink(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.orch = _Orchestrator()
        self.running = {"agent-1": "slug-1"}

        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        self.link = CanaryLink(send, lambda: self.orch, lambda: self.running)

    def _push(self) -> None:
        asyncio.run(self.link.push())

    def _resolve(self, **msg: Any) -> None:
        asyncio.run(self.link.resolve(msg))

    def test_a_running_agents_event_is_sent_once(self) -> None:
        self.orch.events = [_event("e1"), _event("e2", slug="not-running"), "junk", {"id": ""}]
        self._push()
        self._push()
        self.assertEqual([{"type": "canary_fired", "agent_id": "agent-1", "event_id": "e1",
                           "bottle_slug": "slug-1", "reason": "canary token",
                           "request_path": "/x", "fired_at": "2026-11-07T00:00:00+00:00"}],
                         self.sent)

    def test_nothing_running_asks_nothing(self) -> None:
        self.running = {}
        self.orch.fail = True
        self._push()
        self.assertEqual([], self.sent)

    def test_an_unreachable_orchestrator_sends_nothing(self) -> None:
        self.orch.fail = True
        self._push()
        self.assertEqual([], self.sent)

    def test_a_resolution_is_forwarded_and_the_event_can_be_sent_again(self) -> None:
        self.orch.events = [_event("e1")]
        self._push()
        self._resolve(event_id="e1", bottle_slug="slug-1", action="acknowledge")
        self.assertEqual([("e1", {"bottle_slug": "slug-1", "action": "acknowledge"})],
                         self.orch.resolved)
        self._push()  # still unresolved upstream: sent again
        self.assertEqual(2, len(self.sent))

    def test_an_incomplete_resolution_is_ignored(self) -> None:
        self._resolve(event_id="e1", bottle_slug="slug-1")
        self.assertEqual([], self.orch.resolved)

    def test_a_failed_resolution_keeps_the_event_sent(self) -> None:
        self.orch.events = [_event("e1")]
        self._push()
        self.orch.fail = True
        self._resolve(event_id="e1", bottle_slug="slug-1", action="terminate")
        self.orch.fail = False
        self._push()
        self.assertEqual(1, len(self.sent))


if __name__ == "__main__":
    unittest.main()
