"""Unit: what each bottle blocked, in its session status bar (PRD 0099)."""

from __future__ import annotations

import unittest

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.control.hostctl.blocks import BlockNotifier


def _event(host: str = "x.example", code: str | None = "allowlist", *,
           bottle: str = "pi", outcome: str = "blocked") -> AuditEvent:
    payload: dict[str, object] = {"host": host}
    if code is not None:
        payload["reason_code"] = code
    return AuditEvent(type="egress.decision", actor=f"bottled-agent:{bottle}",
                      action="egress.get", outcome=outcome, sensitivity="security",
                      ts_event="2026-10-24T00:00:00Z", bottle=bottle, payload=payload)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.held = ["pi"]
        self.set: dict[tuple[str, str], str] = {}
        self.writes = 0

        def set_option(session: str, name: str, value: str) -> None:
            self.set[(session, name)] = value
            self.writes += 1

        self.notifier = BlockNotifier(lambda: self.held, set_option=set_option,
                                      clock=lambda: self.now)

    def hear(self, *events: AuditEvent, producer: str = "egress") -> None:
        for event in events:
            self.notifier.on_event(event, producer)
        self.notifier.tick()

    def at(self, seconds: float) -> None:
        self.now = 100.0 + seconds
        self.notifier.tick()

    def status(self, name: str, session: str = "pi") -> str:
        return self.set.get((session, f"@bb_{name}"), "")


class Counting(_Case):
    def test_a_block_shows_a_notice_and_a_count(self) -> None:
        self.hear(_event("api.example.com"))
        self.assertEqual("api.example.com · not on allowlist", self.status("block_now"))
        self.assertEqual("api.example.com", self.status("block_host"))
        self.assertEqual("A1", self.status("blocks"))

    def test_counts_are_per_reason_largest_first(self) -> None:
        self.hear(_event(code="secret"), _event(), _event(), _event(code="push"))
        self.assertEqual("A2 S1 P1", self.status("blocks"))

    def test_an_event_without_a_code_counts_as_other(self) -> None:
        self.hear(_event(code=None), _event(code="made-up"))
        self.assertEqual("?2", self.status("blocks"))

    def test_only_gateway_blocks_of_held_sessions_count(self) -> None:
        self.hear(_event(outcome="allowed"), _event(bottle="someone-else"))
        self.hear(_event(), producer="hostctl")
        self.hear(_event(code=None), producer="supervise")  # the gateway reports it
        self.assertEqual({}, self.set)

    def test_a_host_is_escaped_and_shortened(self) -> None:
        self.hear(_event("#[fg=red]" + "a" * 60))
        host = self.status("block_host")
        self.assertTrue(host.startswith("##[fg=red]"))
        self.assertTrue(host.endswith("…"))

    def test_only_changes_are_written(self) -> None:
        self.hear(_event())
        writes = self.writes
        self.notifier.tick()
        self.assertEqual(writes, self.writes)

    def test_an_ended_session_is_forgotten(self) -> None:
        self.hear(_event())
        self.held = []
        self.notifier.tick()
        self.held = ["pi"]
        self.at(60)
        self.hear(_event())
        self.assertEqual("A1", self.status("blocks"))


class Timing(_Case):
    def test_the_notice_clears_after_five_seconds_and_the_count_stays(self) -> None:
        self.hear(_event())
        self.at(4.9)
        self.assertNotEqual("", self.status("block_now"))
        self.at(5)
        self.assertEqual("", self.status("block_now"))
        self.assertEqual("A1", self.status("blocks"))

    def test_a_burst_shows_the_newest_with_the_rest_counted(self) -> None:
        self.hear(_event("a.example"))
        self.at(1)
        self.hear(_event("b.example", "secret"), _event("c.example"))
        self.assertEqual("c.example · not on allowlist (+2)", self.status("block_now"))
        self.assertEqual("A2 S1", self.status("blocks"))

    def test_a_steady_stream_caps_the_notice_then_shows_the_counts(self) -> None:
        for second in range(0, 30):
            self.at(second)
            self.hear(_event())
            showing = self.status("block_now") != ""
            # ten seconds up, five of counts, and again
            self.assertEqual(second % 15 < 10, showing, second)
        self.assertEqual("A30", self.status("blocks"))


if __name__ == "__main__":
    unittest.main()
