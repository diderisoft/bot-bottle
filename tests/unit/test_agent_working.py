"""Unit: whether a bottled agent is working on a prompt (`agent_working`).

Each harness decides from the newest raw lines of its transcript copy
(`AgentProvider.working`): Claude and Codex by their turn markers; a harness
without markers (the default) counts as working while the copy grew within
`WORKING_FOR`. The console link sends each agent's state once per link, then
only its changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.agent_provider import get_provider
from bot_bottle.contrib.claude import transcript as claude
from bot_bottle.contrib.codex import transcript as codex
from bot_bottle.control.hostctl import transcript_serve
from bot_bottle.control.hostctl.transcript_serve import Copy, TranscriptLink, is_working


def _lines(*records: dict[str, Any]) -> list[str]:
    return [json.dumps(r) for r in records]


PROMPT = {"type": "user", "message": {"role": "user", "content": "fix the tests"}}
PROMPT_BLOCKS = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "and open a PR"}]}}
TOOL_CALL = {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}}
TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
REPLY_DONE = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
    {"type": "text", "text": "Done."}]}}
TURN_DURATION = {"type": "system", "subtype": "turn_duration", "durationMs": 4200}
INTERRUPT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "[Request interrupted by user for tool use]"}]}}


class ClaudeTurns(unittest.TestCase):
    def test_a_prompt_starts_a_turn(self) -> None:
        self.assertTrue(claude.working(_lines(PROMPT)))
        self.assertTrue(claude.working(_lines(TURN_DURATION, PROMPT_BLOCKS)))

    def test_a_tool_call_is_mid_turn(self) -> None:
        self.assertTrue(claude.working(_lines(PROMPT, TOOL_CALL, TOOL_RESULT)))

    def test_turn_duration_ends_it(self) -> None:
        self.assertFalse(claude.working(_lines(PROMPT, TOOL_CALL, TOOL_RESULT, TURN_DURATION)))

    def test_a_reply_that_ends_the_turn_ends_it(self) -> None:
        # Before turn_duration is written, or if it never is.
        self.assertFalse(claude.working(_lines(PROMPT, REPLY_DONE)))

    def test_an_interrupt_ends_it(self) -> None:
        self.assertFalse(claude.working(_lines(PROMPT, TOOL_CALL, INTERRUPT)))

    def test_tool_results_bookkeeping_and_subagents_dont_flip_it(self) -> None:
        meta = {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>"}}
        command = {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}}
        compacted = {"type": "user", "isCompactSummary": True,
                     "message": {"content": "This session is being continued…"}}
        sidechain = dict(PROMPT, isSidechain=True)
        for line in (TOOL_RESULT, meta, command, compacted, sidechain,
                     {"type": "mode", "mode": "normal"}):
            with self.subTest(line=line):
                self.assertFalse(claude.working(_lines(PROMPT, TURN_DURATION, line)))

    def test_nothing_said_is_none(self) -> None:
        self.assertIsNone(claude.working([]))
        self.assertIsNone(claude.working(_lines({"type": "ai-title", "aiTitle": "x"})))

    def test_a_cut_or_partial_line_is_skipped(self) -> None:
        lines = ['"stop_reason": "end_turn"}}', *_lines(PROMPT), '{"type": "assist']
        self.assertTrue(claude.working(lines))


class CodexTurns(unittest.TestCase):
    @staticmethod
    def _event(kind: str) -> dict[str, Any]:
        return {"timestamp": "t", "type": "event_msg", "payload": {"type": kind}}

    def test_task_markers(self) -> None:
        self.assertTrue(codex.working(_lines(self._event("task_started"),
                                             self._event("token_count"))))
        self.assertFalse(codex.working(_lines(self._event("task_started"),
                                              self._event("task_complete"))))
        self.assertFalse(codex.working(_lines(self._event("task_started"),
                                              self._event("turn_aborted"))))
        self.assertIsNone(codex.working(_lines(self._event("token_count"))))


class Providers(unittest.TestCase):
    def test_claude_and_codex_decide_others_default(self) -> None:
        self.assertTrue(get_provider("claude").working(_lines(PROMPT)))
        self.assertFalse(get_provider("codex").working(_lines(
            {"type": "event_msg", "payload": {"type": "task_complete"}})))
        # Pi keeps no turn markers: the default, "undecided".
        self.assertIsNone(get_provider("pi").working(_lines(PROMPT)))


class IsWorking(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "session.jsonl"

    def _copy(self, harness: str, records: list[dict[str, Any]], age: float) -> None:
        self.path.write_text("".join(line + "\n" for line in _lines(*records)))
        grew = time.time() - age
        os.utime(self.path, (grew, grew))
        copy = Copy(path=self.path, harness=harness, session="s", activation="a")
        patcher = mock.patch.object(transcript_serve, "find_copy", return_value=copy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_default_goes_by_growth(self) -> None:
        self._copy("pi", [PROMPT], age=5)
        self.assertTrue(is_working("a1", time.time()))
        self.assertFalse(is_working("a1", time.time() + transcript_serve.WORKING_FOR))

    def test_markers_win_over_growth(self) -> None:
        # Just written, but the turn ended; long silent, but mid-turn.
        self._copy("claude", [PROMPT, TURN_DURATION], age=0)
        self.assertFalse(is_working("a1", time.time()))
        self._copy("claude", [PROMPT, TOOL_CALL], age=600)
        self.assertTrue(is_working("a1", time.time()))

    def test_no_copy_says_nothing(self) -> None:
        with mock.patch.object(transcript_serve, "find_copy", return_value=None):
            self.assertIsNone(is_working("a1", time.time()))


class LinkSendsChanges(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.state: dict[str, bool | None] = {"a1": True}

        async def send(msg: dict[str, Any]) -> None:
            self.sent.append(msg)
        self.send = send

        def working(agent_id: str, now: float) -> bool | None:
            del now
            return self.state.get(agent_id)
        patcher = mock.patch.object(transcript_serve, "is_working", side_effect=working)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _push(self, link: TranscriptLink) -> list[dict[str, Any]]:
        before = len(self.sent)
        asyncio.run(link.push_working(["a1", "a2"]))
        return self.sent[before:]

    def test_once_then_only_changes(self) -> None:
        link = TranscriptLink(self.send)
        self.assertEqual([{"type": "agent_working", "agent_id": "a1", "working": True}],
                         self._push(link))  # a2 has no copy: nothing
        self.assertEqual([], self._push(link))
        self.state["a1"] = False
        self.assertEqual([{"type": "agent_working", "agent_id": "a1", "working": False}],
                         self._push(link))

    def test_a_new_link_or_a_restart_sends_it_again(self) -> None:
        link = TranscriptLink(self.send)
        self._push(link)
        # A reconnect is a new link.
        self.assertEqual(1, len(self._push(TranscriptLink(self.send))))
        # An agent started or presented again is forgotten first.
        link.forget("a1")
        self.assertEqual(1, len(self._push(link)))


if __name__ == "__main__":
    unittest.main()
