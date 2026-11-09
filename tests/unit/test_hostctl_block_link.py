"""Unit: block alerts on the console link (block_link.py): what a running
agent's bottle just blocked, merged by host and reason, without paths or
bodies."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.control.hostctl import block_link
from bot_bottle.control.hostctl.block_link import BlockFeed, BlockLink


def _event(host: str = "x.example", code: str | None = "allowlist", *,
           bottle: str = "b-1", outcome: str = "blocked",
           reason: str = "") -> AuditEvent:
    payload: dict[str, object] = {"host": host, "path": "/v1/secret?token=ghp_x"}
    if code is not None:
        payload["reason_code"] = code
    if reason:
        payload["reason"] = reason
    return AuditEvent(type="egress.decision", actor=f"bottled-agent:{bottle}",
                      action="egress.get", outcome=outcome, sensitivity="security",
                      ts_event="2026-11-09T00:00:00Z", bottle=bottle, payload=payload)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.feed = BlockFeed(clock=lambda: self.now)
        self.running = {"a1": "b-1"}
        self.sent: list[dict[str, Any]] = []

    def link(self) -> BlockLink:
        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        return BlockLink(send, lambda: self.running, self.feed)

    def push(self, link: BlockLink) -> list[dict[str, Any]]:
        self.sent.clear()
        asyncio.run(link.push())
        return list(self.sent)


class Feed(_Case):
    def test_only_gateway_blocks(self) -> None:
        self.feed.on_event(_event(outcome="allowed"), "egress")
        self.feed.on_event(_event(), "orchestrator")
        self.feed.on_event(_event(code=None), "supervise")  # a decision, not a block
        self.assertEqual([], self.feed.since(0))
        self.feed.on_event(_event(), "egress")
        self.assertEqual([("b-1", "x.example", "allowlist")],
                         [(b.session, b.host, b.code) for b in self.feed.since(0)])

    def test_detail_for_a_secret_or_a_push_only(self) -> None:
        self.feed.on_event(_event(code="secret",
                                  reason="egress DLP: GitHub token (classic) found in body"),
                           "egress")
        self.feed.on_event(_event(code="allowlist", reason="host not on allowlist"), "egress")
        secret, allowlist = self.feed.since(0)
        self.assertEqual("egress DLP: GitHub token (classic) found in body", secret.detail)
        self.assertEqual("", allowlist.detail)


class Link(_Case):
    def test_merged_by_host_and_reason_for_running_agents(self) -> None:
        link = self.link()
        for host in ("x.example", "x.example", "y.example"):
            self.feed.on_event(_event(host), "egress")
        self.feed.on_event(_event(bottle="b-other"), "egress")  # not this link's agent
        sent = self.push(link)
        self.assertEqual([("a1", "x.example", "allowlist", 2), ("a1", "y.example", "allowlist", 1)],
                         [(m["agent_id"], m["host"], m["reason"], m["count"]) for m in sent])
        self.assertEqual({"type", "agent_id", "host", "reason", "count", "at"}, set(sent[0]))
        self.assertEqual([], self.push(link))  # sent once

    def test_never_a_path_or_a_body(self) -> None:
        link = self.link()
        self.feed.on_event(_event(code="secret", reason="egress DLP: GitHub token"), "egress")
        [msg] = self.push(link)
        self.assertNotIn("ghp_x", repr(msg))
        self.assertEqual("egress DLP: GitHub token", msg["detail"])

    def test_a_new_connection_sends_only_recent_blocks(self) -> None:
        self.feed.add("b-1", "old.example", "allowlist")
        self.now += block_link.RECENT_SECONDS + 1
        self.feed.add("b-1", "new.example", "allowlist")
        self.assertEqual(["new.example"], [m["host"] for m in self.push(self.link())])

    def test_a_flood_waits_for_the_next_push(self) -> None:
        link = self.link()
        for i in range(block_link.SEND_MAX + 5):
            self.feed.add("b-1", f"h{i}.example", "allowlist")
        self.assertEqual(block_link.SEND_MAX, len(self.push(link)))
        self.assertEqual(5, len(self.push(link)))


if __name__ == "__main__":
    unittest.main()
