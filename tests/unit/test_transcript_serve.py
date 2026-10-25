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
from bot_bottle.control.hostctl.transcript_serve import TranscriptReader, find_copy
from bot_bottle.control.hostctl.transcripts import transcripts_root
from . import use_bottle_root

_PI = Path(__file__).parent / "data" / "transcripts" / "pi-branched.jsonl"


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbsrv.")))
        self.addCleanup(use_bottle_root(self.root))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def copy(self, session: str, activation: str, name: str, *, agent_id: str = "c-1",
             data: bytes | None = None, harness: str | None = "pi", opened: float = 1.0,
             guest: str = "") -> Path:
        run = transcripts_root() / session / activation
        run.mkdir(parents=True, exist_ok=True)
        path = run / f"{name}.jsonl"
        path.write_bytes(_PI.read_bytes() if data is None else data)
        guest = guest or f"/home/node/.pi/agent/sessions/--w--/{name.split('.')[0]}.jsonl"
        (run / f"{name}.meta.json").write_text(json.dumps(
            {"guest_path": guest, "offset": 0, "agent_id": agent_id, "opened": opened}))
        follow: dict[str, Any] = {"agent_id": agent_id, "glob": "", "skip": []}
        if harness is not None:
            follow["harness"] = harness
        (run / "follow.json").write_text(json.dumps(follow))
        return path


class TestFindCopy(_Root):
    def test_the_newest_run_and_its_first_file(self) -> None:
        self.copy("s1", "old", "a", opened=1.0)
        first = self.copy("s1", "new", "b", opened=5.0)
        self.copy("s1", "new", "c", opened=6.0)  # a later /fork
        found = find_copy("c-1")
        assert found is not None
        self.assertEqual((first, "pi", "s1", "new"),
                         (found.path, found.harness, found.session, found.activation))
        self.assertIsNone(find_copy("c-2"))
        self.assertIsNone(find_copy(""))

    def test_a_rewritten_file_shows_its_latest_copy(self) -> None:
        self.copy("s1", "a1", "f", opened=1.0)
        rewrite = self.copy("s1", "a1", "f.rewrite-1", opened=2.0,
                            guest="/home/node/.pi/agent/sessions/--w--/f.jsonl")
        found = find_copy("c-1")
        assert found is not None
        self.assertEqual(rewrite, found.path)

    def test_the_harness_falls_back_to_the_path(self) -> None:
        self.copy("s1", "a1", "x", harness=None,
                  guest="/home/node/.claude/projects/-w/x.jsonl")
        found = find_copy("c-1")
        assert found is not None
        self.assertEqual("claude", found.harness)

    def test_no_root(self) -> None:
        self.assertIsNone(find_copy("c-1"))


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
        self.assertEqual({"entries": [], "first": None, "last": None, "available": False},
                         reader.page("c-9"))
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
        self.assertIs(reader.entries("c-1"), reader.entries("c-1"))  # cached
        path.write_bytes(lines[0] + lines[1])
        self.assertEqual(2, len(reader.entries("c-1") or []))

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
        run = transcripts_root() / "s1" / "a1"
        run.mkdir(parents=True)
        (run / "follow.json").write_text(json.dumps({"agent_id": "c-1"}))
        self.assertTrue(TranscriptReader().has_transcript("c-1"))
        self.assertIsNone(TranscriptReader().last("c-1"))

    def test_unreadable_secrets_mask_by_pattern(self) -> None:
        with mock.patch("bot_bottle.secret_source.FileSecrets._load",
                        side_effect=OSError("nope")):
            self.assertEqual({}, transcript_serve._secret_values())  # pylint: disable=protected-access


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

    def test_a_failed_read_is_retried(self) -> None:
        link, sent = self.link()
        link.running["c-1"] = "s1"
        with mock.patch.object(link.transcripts, "last", side_effect=OSError("x")):
            asyncio.run(link.push_transcripts())
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
