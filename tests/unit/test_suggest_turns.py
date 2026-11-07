"""Unit: the conversation with the architect (PRD 0118): turns from a
request, and what the architect's prompt says about them and about edits."""

from __future__ import annotations

import unittest

from bot_bottle.suggest.architect import EDITS_HELP, _build_prompt
from bot_bottle.suggest.turns import MAX_TURNS, Turn, parse_turns, prompt_lines


class TestTurns(unittest.TestCase):
    def test_parse(self) -> None:
        raw = [{"proposal": {"option": "reuse"}, "reply": "r", "events": ["e"]}]
        self.assertEqual([Turn({"option": "reuse"}, "r", ["e"])], parse_turns(raw))
        self.assertEqual([], parse_turns(None))
        for bad in ("x", [1], [{"proposal": {}, "reply": "x" * 3000}],
                    [{"proposal": {}, "reply": "r", "events": [1]}],
                    [{"proposal": {}, "reply": "r"}] * (MAX_TURNS + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_turns(bad)

    def test_prompt_replays_each_turn_without_the_hosts_fields(self) -> None:
        lines = prompt_lines([Turn({"option": "reuse", "source": "architect",
                                    "forges": {"h": {}}}, "add ./main",
                                   ["accepted the edit of ./main"])])
        text = "\n".join(lines)
        self.assertIn("Turn 1, their reply: add ./main", text)
        self.assertIn("Turn 1, what they did: accepted the edit of ./main", text)
        self.assertNotIn("source", text)
        self.assertNotIn('"forges"', text)
        self.assertEqual([], prompt_lines([]))

    def test_the_architect_is_told_about_edits_and_the_conversation(self) -> None:
        prompt = _build_prompt("t", [], {}, [], [], turns=[Turn({}, "use the mirror")])
        self.assertIn(EDITS_HELP, prompt)
        self.assertIn("their reply: use the mirror", prompt)
        self.assertNotIn("conversation so far", _build_prompt("t", [], {}, [], []))


if __name__ == "__main__":
    unittest.main()
