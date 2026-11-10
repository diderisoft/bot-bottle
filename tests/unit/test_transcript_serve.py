"""Unit: the console reads an agent's conversation from its host copy, masked
(PRD 0095)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import remote, transcript_serve
from bot_bottle.control.hostctl.transcript_serve import (
    SESSION_SEQ_STRIDE, TranscriptReader, find_conversation, find_copies, find_copy)
from bot_bottle.control.hostctl.transcripts import TranscriptStore, transcripts_root
from bot_bottle.paths import host_db_path
from . import use_bottle_root

_PI = Path(__file__).parent / "data" / "transcripts" / "pi-branched.jsonl"


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbsrv.")))
        self.addCleanup(use_bottle_root(self.root))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def copy(self, session: str, activation: str, name: str, *, agent_id: str = "c-1",
             data: bytes | None = None, harness: str = "pi", opened: float = 1.0,
             guest: str = "", start: int = 0) -> Path:
        run = transcripts_root() / session / activation
        run.mkdir(parents=True, exist_ok=True)
        path = run / f"{name}.jsonl"
        path.write_bytes(_PI.read_bytes() if data is None else data)
        guest = guest or f"/home/node/.pi/agent/sessions/--w--/{name.split('.')[0]}.jsonl"
        store = TranscriptStore(host_db_path())
        store.put_file(session, activation, path.name, guest_path=guest, offset=0, start=start,
                       opened=opened, agent_id=agent_id, closed=False)
        store.put_run(session, activation, glob="", skip=[], start={}, agent_id=agent_id,
                      harness=harness, closed=False)
        return path


class TestFindCopy(_Root):
    def test_the_file_opened_last(self) -> None:
        self.copy("s1", "old", "a", opened=1.0)
        self.copy("s1", "new", "b", opened=5.0)
        last = self.copy("s1", "new", "c", opened=6.0)  # a later /clear or /fork
        self.copy("s2", "x", "d", opened=9.0, agent_id="c-2")
        found = find_copy("c-1")
        assert found is not None
        self.assertEqual((last, "pi", "s1", "new"),
                         (found.path, found.harness, found.session, found.activation))
        self.assertIsNone(find_copy("c-3"))
        self.assertIsNone(find_copy(""))

    def test_find_copies_returns_all_sessions_oldest_first(self) -> None:
        first = self.copy("s1", "new", "b", opened=5.0)
        last = self.copy("s1", "new", "c", opened=6.0)
        # Copies from a different (older) run are excluded.
        self.copy("s1", "old", "a", opened=1.0)
        copies = find_copies("c-1")
        self.assertEqual([first, last], [c.path for c in copies])

    def test_a_rewritten_file_shows_its_latest_copy(self) -> None:
        self.copy("s1", "a1", "f", opened=1.0)
        rewrite = self.copy("s1", "a1", "f.rewrite-1", opened=2.0,
                            guest="/home/node/.pi/agent/sessions/--w--/f.jsonl")
        found = find_copy("c-1")
        assert found is not None
        self.assertEqual(rewrite, found.path)

    def test_no_root(self) -> None:
        self.assertIsNone(find_copy("c-1"))


class TestAcrossRuns(_Root):
    """A resumed bottle copies only what its agent adds to a file an earlier
    run copied; the conversation is the file's copies put together."""

    def setUp(self) -> None:
        super().setUp()
        self.lines = _PI.read_bytes().splitlines(keepends=True)
        self.guest = "/home/node/.pi/agent/sessions/--w--/f.jsonl"

    def whole(self, upto: int | None = None) -> list[str]:
        reader = TranscriptReader()
        path = self.copy("ref", "r", "f", agent_id="ref", data=b"".join(self.lines[:upto]))
        out = [e.text for e in reader.entries("ref") or []]
        shutil.rmtree(path.parent.parent)
        return out

    def test_a_resumed_runs_copy_continues_the_earlier_one(self) -> None:
        head = b"".join(self.lines[:4])
        self.copy("s1", "a1", "f", data=head, opened=1.0, agent_id="t-old", guest=self.guest)
        self.copy("s1", "a1", "g", data=self.lines[0], opened=2.0, agent_id="t-old",
                  guest="/home/node/.pi/agent/sessions/--w--/g.jsonl")
        newest = self.copy("s1", "a2", "f", data=b"".join(self.lines[4:]), opened=3.0,
                           start=len(head), guest=self.guest)
        conversation = find_conversation("c-1")
        assert conversation is not None
        self.assertEqual(newest, conversation.copy.path)
        self.assertEqual(b"".join(self.lines), conversation.read())
        self.assertEqual(self.whole(), [e.text for e in TranscriptReader().entries("c-1") or []])
        # Another bottle's copy of a file with the same name isn't this one's.
        self.copy("s9", "z", "f", data=b"junk\n", opened=0.5, guest=self.guest, agent_id="x")
        self.assertEqual(b"".join(self.lines), conversation.read())

    def test_overlap_gap_rewrite_and_unreadable_pieces(self) -> None:
        head, rest = b"".join(self.lines[:4]), b"".join(self.lines[4:])
        self.copy("s1", "a1", "f", data=head + self.lines[4], opened=1.0, guest=self.guest)
        self.copy("s1", "a2", "f", data=rest, opened=2.0, start=len(head), guest=self.guest)
        found = find_conversation("c-1")
        assert found is not None
        # The earlier copy read past where the next started: theirs wins.
        self.assertEqual(head + rest, found.read())

        self.copy("s1", "a1", "f", data=head[:-3], opened=1.0, guest=self.guest)
        found = find_conversation("c-1")
        assert found is not None
        # A copy cut short: its cut line is dropped, not glued to the next.
        self.assertEqual(b"".join(self.lines[:3]) + rest, found.read())

        rewrite = self.copy("s1", "a2", "f.rewrite-1", data=self.lines[0], opened=4.0,
                            guest=self.guest)
        found = find_conversation("c-1")
        assert found is not None
        self.assertEqual((self.lines[0], rewrite), (found.read(), found.copy.path))
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("gone")):
            self.assertIsNone(found.read())

    def test_a_continued_copy_after_an_unreadable_one(self) -> None:
        tail = b"".join(self.lines[4:])
        self.copy("s1", "a2", "f", data=tail, opened=2.0, start=99, guest=self.guest)
        found = find_conversation("c-1")
        assert found is not None
        self.assertEqual(tail, found.read())


class TestReader(_Root):
    def test_pages_from_the_newest(self) -> None:
        self.copy("s1", "a1", "f")
        reader = TranscriptReader()
        page = reader.page("c-1", limit=3)
        seqs = [e["seq"] for e in page["entries"]]
        self.assertEqual(3, len(seqs))
        self.assertEqual(page["last"], seqs[-1])
        older = reader.page("c-1", before=seqs[0], limit=500)
        self.assertTrue(all(e["seq"] < seqs[0] for e in older["entries"]))
        self.assertEqual(page["first"], older["entries"][0]["seq"])
        # Only the active branch: the abandoned first prompt isn't served.
        texts = [e.text for e in reader.entries("c-1") or []]
        self.assertFalse(any(t.startswith("Read notes.txt and tell me the secret") for t in texts))

    def test_around_centres_on_a_seq(self) -> None:
        self.copy("s1", "a1", "f")
        reader = TranscriptReader()
        entries = reader.entries("c-1") or []
        middle = entries[len(entries) // 2].seq
        got = [e["seq"] for e in reader.page("c-1", around=middle, limit=4)["entries"]]
        self.assertIn(middle, got)
        self.assertEqual(4, len(got))

    def test_nothing_to_serve(self) -> None:
        reader = TranscriptReader()
        self.assertEqual({"entries": [], "first": None, "last": None, "scope": "all",
                          "available": False}, reader.page("c-9"))
        self.assertIsNone(reader.last("c-9"))
        self.assertFalse(reader.has_transcript("c-9"))

    def test_masks_tokens_and_host_secrets(self) -> None:
        secrets = self.root / "secrets"
        secrets.write_text("MY_KEY=hunter2hunter2\n")
        os.chmod(secrets, 0o600)
        line = json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {
            "role": "toolResult", "toolName": "bash", "content": [{"type": "text", "text":
                "token ghp_" + "A" * 36 + " and hunter2hunter2"}]}}) + "\n"
        self.copy("s1", "a1", "f", data=line.encode())
        with mock.patch.object(transcript_serve, "host_secrets_path", return_value=secrets):
            [entry] = TranscriptReader().entries("c-1") or [None]
        assert entry is not None
        self.assertNotIn("ghp_", entry.text)
        self.assertNotIn("hunter2hunter2", entry.text)

    def test_only_whole_lines_and_reparse_on_growth(self) -> None:
        path = self.copy("s1", "a1", "f", data=b"")
        reader = TranscriptReader()
        lines = _PI.read_bytes().splitlines(keepends=True)
        path.write_bytes(lines[0] + lines[1][:10])
        self.assertEqual(1, len(reader.entries("c-1") or []))
        # Per-copy entries are cached; the combined list is rebuilt each call
        # but parsed entries are reused without re-reading the file.
        self.assertEqual(len(reader.entries("c-1") or []),
                         len(reader.entries("c-1") or []))
        path.write_bytes(lines[0] + lines[1])
        self.assertEqual(2, len(reader.entries("c-1") or []))

    def test_multiple_sessions_are_combined_with_monotonic_seqs(self) -> None:
        # Simulate a /clear: two separate session files for the same agent.
        self.copy("s1", "a1", "sess1",
                  guest="/home/node/.pi/agent/sessions/--w--/sess1.jsonl",
                  opened=1.0)
        second = self.copy("s1", "a1", "sess2",
                           guest="/home/node/.pi/agent/sessions/--w--/sess2.jsonl",
                           opened=2.0)
        reader = TranscriptReader()
        entries = reader.entries("c-1") or []
        seqs = [e.seq for e in entries]
        # Every entry from both sessions appears, in order, each seq once; the
        # second session's start a stride on, so neither's numbers move.
        self.assertGreater(len(seqs), 0)
        self.assertEqual(sorted(set(seqs)), seqs, "seqs increase, each used once")
        self.assertTrue(any(s >= SESSION_SEQ_STRIDE for s in seqs))
        self.assertTrue(any(s < SESSION_SEQ_STRIDE for s in seqs))
        # Reader.last() reports the combined last, so push() fires after /clear.
        first_last = TranscriptReader().last("c-1")
        assert first_last is not None
        # Simulate a new entry arriving in the second session file.
        # parentId must chain from the file's last entry so walk_branch keeps it.
        line = b'{"type":"message","id":"zz","parentId":"478c40e1","message":{"role":"user","content":"hi"}}\n'
        second.write_bytes(second.read_bytes() + line)
        second_last = TranscriptReader().last("c-1")
        assert second_last is not None
        self.assertGreater(second_last, first_last)

    def test_a_seq_names_the_same_entry_as_the_file_grows(self) -> None:
        # The console pages back with `before=<seq>` and reconciles new pages
        # by seq, so a seq must keep naming its entry while the file grows
        # (it used to be a position on the active branch, and moved).
        path = self.copy("s1", "a1", "f", data=b"")
        lines = _PI.read_bytes().splitlines(keepends=True)
        reader = TranscriptReader()
        seen: dict[int, tuple[str, str, str]] = {}
        for n in range(1, len(lines) + 1):
            path.write_bytes(b"".join(lines[:n]))
            for e in reader.entries("c-1") or []:
                key = (e.kind, e.ts, e.text)
                self.assertEqual(key, seen.setdefault(e.seq, key), f"seq {e.seq} at line {n}")
        page = reader.page("c-1", limit=2)
        older = reader.page("c-1", before=page["entries"][0]["seq"], limit=2)
        self.assertTrue(all(e["seq"] < page["entries"][0]["seq"] for e in older["entries"]))

    def test_an_unknown_harness_or_a_vanished_copy(self) -> None:
        path = self.copy("s1", "a1", "f", harness="nope")
        reader = TranscriptReader()
        self.assertEqual([], reader.entries("c-1"))
        self.assertIn("can't read", self.err.getvalue())
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("gone")):
            self.assertIsNone(reader.entries("c-1"))
        shutil.rmtree(path.parent)
        self.assertIsNone(reader.entries("c-1"))

    def test_has_transcript_before_any_copy(self) -> None:
        TranscriptStore(host_db_path()).put_run("s1", "a1", glob="", skip=[], start={},
                                                 agent_id="c-1", harness="pi", closed=False)
        self.assertTrue(TranscriptReader().has_transcript("c-1"))
        self.assertFalse(TranscriptReader().has_transcript("c-2"))
        self.assertIsNone(TranscriptReader().last("c-1"))

    def test_unreadable_secrets_mask_by_pattern(self) -> None:
        with mock.patch("bot_bottle.secret_source.FileSecrets._load",
                        side_effect=OSError("nope")):
            self.assertEqual({}, transcript_serve._secret_values())  # pylint: disable=protected-access


class TestCurrentSession(_Root):
    """After `/clear` the live view shows the current session; a cursor into an
    earlier one (a search hit from before the `/clear`) reads the full history,
    and filtering never renumbers a seq."""

    def two_sessions(self) -> TranscriptReader:
        self.copy("s1", "a1", "sess1", guest="/home/node/.pi/agent/sessions/--w--/sess1.jsonl",
                  opened=1.0)
        self.copy("s1", "a1", "sess2", guest="/home/node/.pi/agent/sessions/--w--/sess2.jsonl",
                  opened=2.0)
        return TranscriptReader()

    def test_current_only_is_the_newest_session_with_the_same_seqs(self) -> None:
        reader = self.two_sessions()
        everything = reader.entries("c-1") or []
        current = reader.entries("c-1", current_only=True) or []
        self.assertTrue(current)
        self.assertTrue(all(e.seq >= SESSION_SEQ_STRIDE for e in current))
        by_seq = {e.seq: (e.kind, e.text) for e in everything}
        for e in current:
            self.assertEqual(by_seq[e.seq], (e.kind, e.text), f"seq {e.seq} renumbered")
        self.assertLess(len(current), len(everything))

    def test_page_defaults_to_the_current_session(self) -> None:
        reader = self.two_sessions()
        page = reader.page("c-1", limit=500)
        self.assertEqual("current", page["scope"])
        self.assertGreaterEqual(page["first"], SESSION_SEQ_STRIDE)
        self.assertTrue(all(e["seq"] >= SESSION_SEQ_STRIDE for e in page["entries"]))
        whole = reader.page("c-1", limit=500, full=True)
        self.assertEqual("all", whole["scope"])
        self.assertLess(whole["first"], SESSION_SEQ_STRIDE)
        self.assertEqual(page["last"], whole["last"])

    def test_a_search_hit_before_the_clear_is_reachable(self) -> None:
        reader = self.two_sessions()
        hits = [m for m in reader.search("c-1", "line two") if m["seq"] < SESSION_SEQ_STRIDE]
        self.assertTrue(hits, "the first session has a hit")
        seq = hits[0]["seq"]
        page = reader.page("c-1", around=seq, limit=4)
        self.assertEqual("all", page["scope"])
        self.assertIn(seq, [e["seq"] for e in page["entries"]])
        # Paging back from there stays in the full history.
        back = reader.page("c-1", before=seq, limit=500)
        self.assertEqual("all", back["scope"])
        self.assertTrue(all(e["seq"] < seq for e in back["entries"]))

    def test_an_empty_current_session_shows_the_history(self) -> None:
        self.copy("s1", "a1", "sess1", guest="/home/node/.pi/agent/sessions/--w--/sess1.jsonl",
                  opened=1.0)
        self.copy("s1", "a1", "sess2", data=b"",
                  guest="/home/node/.pi/agent/sessions/--w--/sess2.jsonl", opened=2.0)
        page = TranscriptReader().page("c-1", limit=500)
        self.assertEqual("all", page["scope"])
        self.assertTrue(page["entries"])

    def test_seqs_stay_put_as_the_current_session_grows(self) -> None:
        self.copy("s1", "a1", "sess1", guest="/home/node/.pi/agent/sessions/--w--/sess1.jsonl",
                  opened=1.0)
        second = self.copy("s1", "a1", "sess2", data=b"",
                           guest="/home/node/.pi/agent/sessions/--w--/sess2.jsonl", opened=2.0)
        lines = _PI.read_bytes().splitlines(keepends=True)
        reader = TranscriptReader()
        seen: dict[int, tuple[str, str]] = {}
        for n in range(1, len(lines) + 1):
            second.write_bytes(b"".join(lines[:n]))
            for full in (False, True):
                for e in reader.page("c-1", limit=500, full=full)["entries"]:
                    key = (e["kind"], e["text"])
                    self.assertEqual(key, seen.setdefault(e["seq"], key), f"seq {e['seq']}")


class TestSearch(_Root):
    def test_finds_the_newest_matches_with_a_snippet(self) -> None:
        self.copy("s1", "a1", "f")
        reader = TranscriptReader()
        matches = reader.search("c-1", "LINE TWO")
        self.assertTrue(matches)
        self.assertEqual(sorted((m["seq"] for m in matches), reverse=True),
                         [m["seq"] for m in matches])
        self.assertIn("Line two", matches[0]["snippet"])
        self.assertEqual([], reader.search("c-1", "  "))
        self.assertEqual([], reader.search("c-9", "x"))

    def test_kinds_filter_and_meta(self) -> None:
        self.copy("s1", "a1", "f")
        reader = TranscriptReader()
        kinds = {m["kind"] for m in reader.search("c-1", "line two", kinds=["tool_result"])}
        self.assertEqual({"tool_result"}, kinds)
        self.assertTrue(reader.search("c-1", "session started", kinds=["system"]))
        line = json.dumps({"type": "custom", "id": "z", "parentId": None}) + "\n"
        claude_meta = json.dumps({"type": "mode", "mode": "normal"}) + "\n"
        self.copy("s2", "a1", "g", agent_id="c-2", data=(line + claude_meta).encode(),
                  harness="claude", guest="/home/node/.claude/projects/-w/g.jsonl")
        self.assertEqual([], reader.search("c-2", "mode: normal"))
        self.assertEqual(1, len(reader.search("c-2", "mode: normal", kinds=["meta"])))

    def test_long_text_is_cut_around_the_match(self) -> None:
        text = "a" * 300 + "needle" + "b" * 300
        line = json.dumps({"type": "message", "id": "m", "parentId": None, "message": {
            "role": "user", "content": text}}) + "\n"
        self.copy("s1", "a1", "f", data=line.encode())
        [match] = TranscriptReader().search("c-1", "needle", limit=5)
        self.assertTrue(match["snippet"].startswith("…") and match["snippet"].endswith("…"))
        self.assertLess(len(match["snippet"]), 140)


class TestOnTheLink(_Root):
    def link(self) -> tuple[remote.ConsoleLink, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        return remote.ConsoleLink(mock.MagicMock(), send, agents=lambda: []), sent

    def test_transcript_request(self) -> None:
        self.copy("s1", "a1", "f")
        link, sent = self.link()
        asyncio.run(link.handle({"type": "transcript", "agent_id": "c-1", "limit": 2,
                                 "request_id": "r1"}))
        reply = sent[0]
        self.assertEqual(("transcript_entries", "r1", "c-1", 2),
                         (reply["type"], reply["request_id"], reply["agent_id"],
                          len(reply["entries"])))
        # A bad `before` is ignored, not an error.
        asyncio.run(link.handle({"type": "transcript", "agent_id": "c-1", "before": "x"}))
        self.assertEqual(reply["last"], sent[1]["entries"][-1]["seq"])
        # `all: true` asks for the full history.
        asyncio.run(link.handle({"type": "transcript", "agent_id": "c-1", "all": True}))
        self.assertEqual("all", sent[2]["scope"])

    def test_appended_is_sent_once_per_change(self) -> None:
        path = self.copy("s1", "a1", "f")
        link, sent = self.link()
        link.running["c-1"] = "s1"
        link.running["c-2"] = "s2"  # no copy yet
        asyncio.run(link.push_transcripts())
        asyncio.run(link.push_transcripts())
        self.assertEqual(1, len(sent))
        self.assertEqual("transcript_appended", sent[0]["type"])
        with path.open("ab") as f:
            f.write(b'{"type":"label","id":"zz","parentId":"478c40e1"}\n')
        asyncio.run(link.push_transcripts())
        self.assertEqual(2, len(sent))
        self.assertGreater(sent[1]["last"], sent[0]["last"])

    def test_search_request(self) -> None:
        self.copy("s1", "a1", "f")
        link, sent = self.link()
        asyncio.run(link.handle({"type": "transcript_search", "agent_id": "c-1",
                                 "query": "line two", "kinds": ["tool_result"],
                                 "request_id": "r2"}))
        self.assertEqual(("transcript_matches", "r2"), (sent[0]["type"], sent[0]["request_id"]))
        self.assertTrue(sent[0]["matches"])
        asyncio.run(link.handle({"type": "transcript_search", "agent_id": "c-1", "query": 3}))
        self.assertEqual([], sent[1]["matches"])

    def test_a_failed_read_is_retried(self) -> None:
        link, sent = self.link()
        link.running["c-1"] = "s1"
        with mock.patch.object(link.transcripts.reader, "last", side_effect=OSError("x")):
            asyncio.run(link.push_transcripts())
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
