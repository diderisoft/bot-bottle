"""A prompt typed while Claude is busy is written as a `queued_command`
attachment when Claude takes it up; it's the person's turn, so the console
shows it (and its pending bubble settles) like any other prompt."""

from __future__ import annotations

import json
import unittest
from typing import Any

from bot_bottle.contrib.claude import transcript as claude

_QUEUED = {"parentUuid": "p1", "isSidechain": False,
           "attachment": {"type": "queued_command",
                          "prompt": "Also curl https://example.com to check it is reachable.",
                          "commandMode": "prompt"},
           "type": "attachment", "uuid": "a1", "timestamp": "2026-11-03T00:48:55.794Z"}


def _line(raw: dict[str, Any]) -> str:
    return json.dumps(raw)


class TestQueuedPrompt(unittest.TestCase):
    def test_a_queued_prompt_is_a_user_turn(self) -> None:
        line = claude.parse_line(dict(_QUEUED))
        self.assertEqual([("user", "Also curl https://example.com to check it is reachable.")],
                         [(p.kind, p.text) for p in line.parts])
        self.assertFalse(line.parts[0].meta)
        self.assertEqual(("a1", "p1"), (line.id, line.parent))

    def test_other_attachments_stay_meta(self) -> None:
        for att in ({"type": "todo_reminder"}, {"type": "queued_command", "prompt": "ls",
                                                "commandMode": "bash"},
                    {"type": "queued_command", "prompt": "   "}):
            with self.subTest(att=att):
                parts = claude.parse_line({"type": "attachment", "uuid": "x",
                                           "attachment": att}).parts
                self.assertTrue(all(p.meta for p in parts))
                self.assertEqual(["system"], [p.kind for p in parts])

    def test_it_is_on_the_active_branch_in_order(self) -> None:
        lines = [
            _line({"type": "user", "uuid": "u1", "parentUuid": None,
                   "message": {"role": "user", "content": "Build a FastAPI todo API."}}),
            _line({"type": "assistant", "uuid": "p1", "parentUuid": "u1",
                   "message": {"role": "assistant", "stop_reason": "tool_use",
                               "content": [{"type": "text", "text": "On it."}]}}),
            _line(_QUEUED),
            _line({"type": "assistant", "uuid": "r1", "parentUuid": "a1",
                   "message": {"role": "assistant", "stop_reason": "end_turn",
                               "content": [{"type": "text", "text": "example.com is blocked."}]}}),
        ]
        shown = [(e.kind, e.text) for e in claude.active_branch(claude.parse(lines)) if not e.meta]
        queued = ("user", "Also curl https://example.com to check it is reachable.")
        self.assertIn(queued, shown)
        self.assertLess(shown.index(queued), shown.index(("assistant", "example.com is blocked.")))

    def test_taking_it_up_means_working(self) -> None:
        done = _line({"type": "system", "subtype": "turn_duration", "uuid": "s1"})
        self.assertTrue(claude.working([done, _line(_QUEUED)]))
        self.assertFalse(claude.working([_line(_QUEUED), done]))


if __name__ == "__main__":
    unittest.main()
