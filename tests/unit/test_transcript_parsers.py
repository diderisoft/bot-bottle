"""Unit: Claude's and Codex's transcripts parse into entries (PRD 0095), from
real sessions of the pinned harness versions (secrets and vendor prompts
scrubbed)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.agent_provider import get_provider
from bot_bottle.contrib.claude import transcript as claude
from bot_bottle.contrib.codex import transcript as codex
from bot_bottle.transcript import TranscriptEntry, entries, walk_branch

_DATA = Path(__file__).parent / "data" / "transcripts"


def _lines(name: str) -> list[str]:
    return (_DATA / name).read_text(encoding="utf-8").splitlines(keepends=True)


def _plan(workdir: str = "/home/node/workspace", env: dict[str, str] | None = None) -> mock.Mock:
    plan = mock.Mock(guest_home="/home/node")
    plan.workspace_plan.workdir = workdir
    plan.agent_provision.env_vars = env or {}
    return plan


def _shown(entries_: list[TranscriptEntry]) -> list[TranscriptEntry]:
    return [e for e in entries_ if not e.meta]


class TestClaude(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = get_provider("claude")
        self.entries = self.provider.parse_transcript(_lines("claude-rewound.jsonl"))

    def test_glob(self) -> None:
        self.assertEqual("/home/node/.claude/projects/-home-node-workspace/*.jsonl",
                         self.provider.transcript_glob(_plan()))
        self.assertEqual("-a-b-c-d", claude.project_dir_name("/a.b_c d"))

    def test_tools_calls_and_results_pair_up(self) -> None:
        calls = [(e.tool, json.loads(e.text)) for e in self.entries if e.kind == "tool_call"]
        self.assertEqual(("Read", {"file_path": "/home/node/workspace/notes.txt"}), calls[0])
        results = [e for e in self.entries if e.kind == "tool_result"]
        self.assertEqual(["Read", "Bash", "Bash", "Write", "Read"], [r.tool for r in results])
        self.assertIn("pelican", results[0].text)
        self.assertIn("not in the bottle's egress.routes", results[2].text)
        self.assertEqual([False] * 4 + [True], [r.error for r in results])

    def test_bookkeeping_is_meta(self) -> None:
        meta = {e.text.split(":")[0] for e in self.entries if e.meta}
        self.assertTrue({"mode", "permission mode", "file history snapshot", "title",
                         "attachment"} <= meta)
        self.assertTrue(any(e.thinking for e in self.entries))

    def test_compacting_keeps_the_history_before_it(self) -> None:
        # Compacting writes a boundary line with no parentUuid; it names the
        # line it follows in logicalParentUuid, and the walk goes through it.
        lines = [json.dumps(r) + "\n" for r in (
            {"type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "t1",
             "message": {"role": "user", "content": "first task"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "t2",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
            {"type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None,
             "logicalParentUuid": "a1", "timestamp": "t3", "content": "Conversation compacted"},
            {"type": "user", "uuid": "u2", "parentUuid": "b1", "timestamp": "t4",
             "message": {"role": "user", "content": "next task"}},
        )]
        active = self.provider.active_entries(self.provider.parse_transcript(lines))
        texts = [e.text for e in active if e.kind in ("user", "assistant")]
        self.assertEqual(["first task", "done", "next task"], texts)

    def test_the_active_branch_is_the_rewound_one(self) -> None:
        branch = _shown(self.provider.active_entries(self.entries))
        users = [e.text for e in branch if e.kind == "user"]
        self.assertEqual(2, len(users))
        self.assertTrue(users[0].startswith("Read notes.txt"))
        self.assertEqual("Instead, just tell me how many lines notes.txt has.", users[1])
        # Both parallel results stay, though each hangs off its own call.
        self.assertEqual(3, len([e for e in branch if e.kind == "tool_result"]))
        self.assertFalse(any("out.txt" in e.text for e in branch))

    def test_other_shapes(self) -> None:
        parsed = claude.parse([
            json.dumps({"type": "user", "uuid": "u1", "message": {"content": "<command-name>/x"}}),
            json.dumps({"type": "user", "uuid": "u2", "isMeta": True,
                        "message": {"content": [{"type": "text", "text": "caveat"}]}}),
            json.dumps({"type": "user", "uuid": "u3", "message": {"content": [
                {"type": "image"}, "junk"]}}),
            json.dumps({"type": "assistant", "uuid": "a1", "isApiErrorMessage": True,
                        "message": {"content": "overloaded"}}),
            json.dumps({"type": "assistant", "uuid": "a2", "message": {"content": [
                {"type": "server_thing"}, "junk"]}}),
            json.dumps({"type": "system", "subtype": "api_error", "level": "error",
                        "content": "retrying"}),
            json.dumps({"type": "summary", "summary": "earlier work"}),
            json.dumps({"type": "user", "isSidechain": True, "message": {"content": "sub"}}),
            json.dumps({"type": "queue-operation", "operation": "enqueue"}),
            json.dumps({"type": "last-prompt", "lastPrompt": "can you rebase", "leafUuid": "x"}),
            json.dumps({"type": "new-kind", "uuid": "n1", "parentUuid": "a2"}),
        ])
        self.assertEqual(
            [("system", True), ("system", True), ("user", False), ("assistant", False),
             ("system", False), ("system", False), ("system", False), ("user", True),
             ("system", True), ("system", True), ("system", False)],
            [(e.kind, e.meta) for e in parsed])
        # Image block with no source → placeholder text, no url
        self.assertEqual("[image]", parsed[2].text)
        self.assertEqual("", parsed[2].image_url)
        self.assertTrue(parsed[3].error and parsed[5].error)
        # Bookkeeping of a type not known is named, not dumped; a conversation
        # line of one is shown whole.
        self.assertEqual(["queue-operation", "last-prompt"], [parsed[8].text, parsed[9].text])
        self.assertIn('"new-kind"', parsed[10].text)

    def test_image_url_in_user_message(self) -> None:
        png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        parsed = claude.parse([
            json.dumps({"type": "user", "uuid": "u1", "message": {"content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": png_b64}}]}}),
            json.dumps({"type": "user", "uuid": "u2", "message": {"content": [
                {"type": "image", "source": {"type": "url",
                                             "url": "https://example.com/img.png"}}]}}),
            json.dumps({"type": "user", "uuid": "u3", "message": {"content": [
                {"type": "text", "text": "see this:"},
                {"type": "image", "source": {"type": "url",
                                             "url": "https://example.com/img.png"}}]}}),
        ])
        self.assertEqual(f"data:image/png;base64,{png_b64}", parsed[0].image_url)
        self.assertEqual("", parsed[0].text)
        self.assertEqual("https://example.com/img.png", parsed[1].image_url)
        # Mixed text + image in one message: merged into one entry
        self.assertEqual("see this:", parsed[2].text)
        self.assertEqual("https://example.com/img.png", parsed[2].image_url)
        self.assertEqual(3, len(parsed))  # 3 user messages → 3 entries

    def test_image_url_in_assistant_message(self) -> None:
        url = "https://example.com/result.png"
        parsed = claude.parse([
            json.dumps({"type": "assistant", "uuid": "a1", "message": {"content": [
                {"type": "image", "source": {"type": "url", "url": url}}]}}),
        ])
        self.assertEqual(url, parsed[0].image_url)
        self.assertEqual("", parsed[0].text)

    def test_image_url_in_tool_result(self) -> None:
        png_b64 = "abc123"
        parsed = claude.parse([
            json.dumps({"type": "user", "uuid": "u1", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "screenshot:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": png_b64}}]}]}}),
        ])
        self.assertEqual("tool_result", parsed[0].kind)
        self.assertEqual("screenshot:", parsed[0].text)
        self.assertEqual(f"data:image/png;base64,{png_b64}", parsed[0].image_url)


class TestCodex(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = get_provider("codex")

    def test_glob_follows_codex_home(self) -> None:
        self.assertEqual("/home/node/.codex/sessions/*/*/*/rollout-*.jsonl",
                         self.provider.transcript_glob(_plan()))
        self.assertEqual("/x/sessions/*/*/*/rollout-*.jsonl",
                         self.provider.transcript_glob(_plan(env={"CODEX_HOME": "/x"})))

    def test_a_real_rollout(self) -> None:
        shown = _shown(self.provider.parse_transcript(_lines("codex.jsonl")))
        self.assertEqual(["Read and follow the instructions in /home/node/.bot-bottle-prompt.txt.",
                          "Read notes.txt and tell me the secret word. Then run the shell "
                          "command: ls -la && uname -s. Then run: curl -sS -m 10 "
                          "https://example.com | head -5 and tell me what happened. Do not "
                          "request egress access, just report."],
                         [e.text for e in shown if e.kind == "user"])
        results = [e for e in shown if e.kind == "tool_result"]
        self.assertEqual({"exec"}, {r.tool for r in results})
        self.assertIn("not in the bottle's egress.routes", results[-1].text)
        self.assertTrue(shown[-1].text.startswith("The secret word is **pelican**"))
        # The TUI's copies of each message aren't shown twice.
        self.assertEqual(1, len([e for e in shown if "secret word is **pelican**" in e.text]))
        # Injected context and developer instructions are bookkeeping.
        self.assertFalse(any("<environment_context>" in e.text for e in shown))

    def test_an_edited_message_is_a_new_rollout(self) -> None:
        shown = _shown(self.provider.parse_transcript(_lines("codex-forked.jsonl")))
        self.assertEqual("Instead, just tell me how many lines notes.txt has.",
                         [e.text for e in shown if e.kind == "user"][-1])
        self.assertEqual("`notes.txt` has 2 lines.", shown[-1].text)
        self.assertTrue(any(e.thinking for e in shown))
        # No tree: every entry is active.
        entries_ = self.provider.parse_transcript(_lines("codex-forked.jsonl"))
        self.assertEqual(entries_, self.provider.active_entries(entries_))

    def test_other_shapes(self) -> None:
        def item(**p: object) -> str:
            return json.dumps({"timestamp": "t", "type": "response_item", "payload": p})
        parsed = codex.parse([
            item(type="function_call", name="shell", arguments='{"cmd":"ls"}', call_id="c1"),
            item(type="function_call_output", call_id="c1",
                 output={"content": "nope", "success": False}),
            item(type="local_shell_call", action={"command": ["ls"]}),
            item(type="web_search_call", action={"query": "q"}),
            item(type="message", role="user", content=[{"type": "input_image"}]),
            item(type="mystery"),
            json.dumps({"type": "event_msg", "payload": {"type": "error", "message": "boom"}}),
            json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted",
                                                         "reason": "interrupted"}}),
            json.dumps({"type": "compacted", "payload": {"message": "summary"}}),
            json.dumps({"type": "turn_context", "payload": {}}),
        ])
        self.assertEqual(["tool_call", "tool_result", "tool_call", "tool_call", "user", "system",
                          "system", "system", "system", "system"], [e.kind for e in parsed])
        self.assertEqual(("shell", True, "nope"), (parsed[1].tool, parsed[1].error, parsed[1].text))
        self.assertEqual(("shell", "web_search"), (parsed[2].tool, parsed[3].tool))
        self.assertEqual("[image]", parsed[4].text)
        self.assertTrue(parsed[6].error)
        self.assertEqual("turn aborted: interrupted", parsed[7].text)
        self.assertTrue(parsed[9].meta)


class TestShared(unittest.TestCase):
    def test_wire_form_leaves_defaults_out(self) -> None:
        entry = TranscriptEntry(seq=1, ts="t", kind="tool_result", text="x", raw_offset=3,
                                tool="bash", error=True)
        self.assertEqual({"seq": 1, "ts": "t", "kind": "tool_result", "text": "x",
                          "raw_offset": 3, "tool": "bash", "error": True}, entry.to_json())

    def test_wire_form_includes_image_url_when_set(self) -> None:
        entry = TranscriptEntry(seq=1, ts="t", kind="user", text="", raw_offset=0,
                                image_url="data:image/png;base64,abc")
        wire = entry.to_json()
        self.assertEqual("data:image/png;base64,abc", wire["image_url"])
        # Omitted when empty
        plain = TranscriptEntry(seq=1, ts="t", kind="user", text="hi", raw_offset=0)
        self.assertNotIn("image_url", plain.to_json())

    def test_walk_without_ids_keeps_everything(self) -> None:
        parsed = entries(["x\n", "y\n"], lambda raw: None)  # type: ignore[arg-type,return-value]
        self.assertEqual(parsed, walk_branch(parsed))


if __name__ == "__main__":
    unittest.main()
