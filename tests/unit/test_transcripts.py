"""Unit: a session's harness transcript is copied to the host (PRD 0095).

The follower's poll script runs in a local `sh` against files in a temp dir,
standing in for the bottle's `exec`."""
# pylint: disable=protected-access

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from bot_bottle.agent_provider import AgentProvider, get_provider
from bot_bottle.backend.base import ExecResult
from bot_bottle.contrib.pi import transcript as pi_transcript
from bot_bottle.control.hostctl import transcripts
from bot_bottle.control.hostctl.audit_events import HostAudit
from bot_bottle.control.hostctl.audit_ocsf import to_ocsf
from bot_bottle.control.hostctl.transcripts import TranscriptFollower
from . import use_bottle_root

_FIXTURE = Path(__file__).parent / "data" / "transcripts" / "pi-branched.jsonl"


class LocalBottle:
    """Runs `exec` scripts in a local shell."""

    def __init__(self) -> None:
        self.fail = False

    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        del user
        if self.fail:
            raise RuntimeError("bottle gone")
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
        return ExecResult(r.returncode, r.stdout, r.stderr)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbtx.")))
        self.addCleanup(use_bottle_root(self.root / "bb"))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.guest = self.root / "guest dir"  # a space, to check the quoting
        self.guest.mkdir()
        self.glob = f"{self.guest}/*.jsonl"
        self.audit = mock.MagicMock(spec=HostAudit)
        self.bottle = LocalBottle()

    def begin(self) -> TranscriptFollower:
        return TranscriptFollower.begin(cast(Any, self.bottle), session="s1", activation="a1",
                                        glob=self.glob, agent_id="t-abc", audit=self.audit)

    def copy_dir(self) -> Path:
        return transcripts.copy_dir("s1", "a1")

    def captured(self) -> list[dict[str, Any]]:
        return [c.kwargs for c in self.audit.transcript_captured.call_args_list]


class TestPiTranscript(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = _FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)

    def test_glob_follows_the_working_directory(self) -> None:
        self.assertEqual("/home/node/.pi/agent/sessions/--home-node-workspace--/*.jsonl",
                         pi_transcript.transcript_glob("/home/node/", "/home/node/workspace"))
        self.assertEqual("--C--x-y--", pi_transcript.session_dir_name("C:\\x/y"))

    def test_the_provider_globs_its_plans_workdir(self) -> None:
        plan = mock.Mock(guest_home="/home/node")
        plan.workspace_plan.workdir = "/home/node"
        self.assertEqual("/home/node/.pi/agent/sessions/--home-node--/*.jsonl",
                         get_provider("pi").transcript_glob(plan))

    def test_a_real_session_parses_into_every_kind(self) -> None:
        entries = get_provider("pi").parse_transcript(self.lines)
        kinds = {e.kind for e in entries}
        self.assertEqual({"user", "assistant", "tool_call", "tool_result", "system"}, kinds)
        self.assertEqual(list(range(len(entries))), [e.seq for e in entries])
        calls = [(e.tool, json.loads(e.text)) for e in entries if e.kind == "tool_call"]
        self.assertEqual(("read", {"path": "notes.txt"}), calls[0])
        self.assertEqual("bash", calls[1][0])
        results = [e for e in entries if e.kind == "tool_result"]
        self.assertIn("pelican", results[0].text)
        # The blocked egress, as the tool reported it.
        self.assertTrue(any("not in the bottle's egress.routes" in r.text for r in results))
        # Every line's entries point at where the line starts.
        offsets = sorted({e.raw_offset for e in entries})
        starts, at = [], 0
        for line in self.lines:
            starts.append(at)
            at += len(line.encode("utf-8"))
        self.assertEqual(starts, offsets)
        # Thinking is its own entry, never mixed into a reply's text.
        thinking = [e for e in entries if e.thinking]
        self.assertTrue(thinking[0].text.startswith("The user wants me to"))
        self.assertEqual({"assistant"}, {e.kind for e in thinking})
        self.assertFalse(any("The user wants me to" in e.text for e in entries if not e.thinking))

    def test_the_active_branch_is_the_edited_resubmission(self) -> None:
        branch = pi_transcript.active_branch(pi_transcript.parse(self.lines))
        users = [e.text for e in branch if e.kind == "user"]
        self.assertEqual(["Read notes.txt and tell me only the second line, nothing else."], users)
        self.assertTrue(branch[0].text.startswith("session started in /home/node/workspace"))
        self.assertTrue(any(e.text.startswith("The user explored a different") for e in branch))
        self.assertEqual("$ echo branch-check\nbranch-check\n", branch[-1].text)

    def test_numbering_continues_from_where_the_last_read_stopped(self) -> None:
        first = pi_transcript.parse(self.lines[:3])
        at = sum(len(line.encode()) for line in self.lines[:3])
        rest = pi_transcript.parse(self.lines[3:], offset=at, seq=len(first))
        self.assertEqual(pi_transcript.parse(self.lines), first + rest)

    def test_unknown_and_broken_lines_are_kept(self) -> None:
        entries = pi_transcript.parse([
            '{"type":"label","id":"x1","parentId":null,"targetId":"a","label":"l"}\n',
            "not json\n",
            '{"type":"custom_message","id":"x2","parentId":"x1","content":"hi"}\n',
            '{"type":"session_info","id":"x3","parentId":"x2","name":"n"}\n',
            '{"type":"message","id":"x4","parentId":"x3","message":{"role":"assistant",'
            '"content":[],"stopReason":"error","errorMessage":"rate limited"}}\n',
            '{"type":"message","id":"x5","parentId":"x4","message":{"role":"custom",'
            '"content":"c"}}\n',
            '{"type":"message","id":"x6","parentId":"x5","message":{"role":"user",'
            '"content":"plain"}}\n',
            '{"type":"message","id":"x7","parentId":"x6","message":{"role":"user",'
            '"content":[{"type":"image"},{"type":"text","text":"see"}]}}\n',
            '{"type":"compaction","id":"x8","parentId":"x7","summary":"short"}\n',
        ])
        self.assertEqual(["system"] * 4 + ["assistant", "system", "user", "user", "system"],
                         [e.kind for e in entries])
        self.assertIn('"label": "l"', entries[0].text)
        self.assertEqual("not json", entries[1].text)
        self.assertEqual(("rate limited", True), (entries[4].text, entries[4].error))
        self.assertEqual("[image]see", entries[7].text)
        self.assertEqual("short", entries[8].text)

    def test_a_failed_bang_command_is_an_error(self) -> None:
        entry = pi_transcript.parse([json.dumps({"type": "message", "id": "b", "message": {
            "role": "bashExecution", "command": "false", "output": "", "exitCode": 1}}) + "\n"])[0]
        self.assertTrue(entry.error)


class TestDefaultProvider(unittest.TestCase):
    def test_no_transcript_and_lines_as_system(self) -> None:
        provider = cast(Any, AgentProvider)
        self.assertIsNone(provider.transcript_glob(provider, mock.Mock()))
        entries = provider.parse_transcript(provider, ["é\n", "b\n"], offset=10, seq=2)
        self.assertEqual([(2, 10, "é"), (3, 13, "b")],
                         [(e.seq, e.raw_offset, e.text) for e in entries])


class TestFollower(_Root):
    def test_copies_new_files_only_as_they_grow(self) -> None:
        (self.guest / "old.jsonl").write_text("before the session\n")
        follower = self.begin()
        follower.poll(cast(Any, self.bottle))
        self.assertEqual([], list(self.copy_dir().glob("*.jsonl")))
        new = self.guest / "s.jsonl"
        new.write_bytes(b'{"a":1}\n{"b"')  # mid-line
        follower.poll(cast(Any, self.bottle))
        with new.open("ab") as f:
            f.write(b':2}\n')
        follower.poll(cast(Any, self.bottle))
        copy = self.copy_dir() / "s.jsonl"
        self.assertEqual(b'{"a":1}\n{"b":2}\n', copy.read_bytes())
        self.assertEqual(0o600, copy.stat().st_mode & 0o777)
        for d in (transcripts.transcripts_root(), self.copy_dir().parent, self.copy_dir()):
            self.assertEqual(0o700, d.stat().st_mode & 0o777)
        meta = json.loads((self.copy_dir() / "s.meta.json").read_text())
        self.assertEqual({"guest_path": str(new), "offset": 16, "agent_id": "t-abc"}, meta)
        self.assertEqual([str(self.guest / "old.jsonl")],
                         json.loads((self.copy_dir() / "follow.json").read_text())["skip"])

    def test_close_reads_once_more_and_checkpoints_the_digest(self) -> None:
        follower = self.begin()
        follower.start(cast(Any, self.bottle))
        (self.guest / "s.jsonl").write_bytes(b"line\n")
        follower.close()
        [final] = self.captured()
        self.assertEqual({"bottle": "s1", "activation": "a1", "copy": "s1/a1/s.jsonl",
                          "guest_path": str(self.guest / "s.jsonl"), "size": 5,
                          "sha256": hashlib.sha256(b"line\n").hexdigest(), "final": True}, final)
        self.assertTrue(json.loads((self.copy_dir() / "s.meta.json").read_text())["closed"])
        self.assertIsNone(TranscriptFollower.resume(session="s1", activation="a1",
                                                    audit=self.audit))

    def test_checkpoints_every_five_minutes_while_it_grows(self) -> None:
        clock = Clock()
        follower = TranscriptFollower(session="s1", activation="a1", glob=self.glob,
                                      agent_id="t", skip=[], audit=self.audit, clock=clock)
        path = self.guest / "s.jsonl"
        path.write_text("1\n")
        follower.poll(cast(Any, self.bottle))
        clock.now += 299
        path.write_text("1\n2\n")
        follower.poll(cast(Any, self.bottle))
        self.assertEqual([], self.captured())
        clock.now += 1
        follower.poll(cast(Any, self.bottle))
        self.assertEqual([(4, False)], [(c["size"], c["final"]) for c in self.captured()])
        clock.now += 600  # no growth, no checkpoint
        follower.poll(cast(Any, self.bottle))
        self.assertEqual(1, len(self.captured()))

    def test_an_edit_before_the_offset_starts_a_new_copy(self) -> None:
        follower = self.begin()
        path = self.guest / "s.jsonl"
        path.write_text("one\ntwo\n")
        follower.poll(cast(Any, self.bottle))
        path.write_text("ONE\ntwo\nthree\n")  # same length so far, different bytes
        follower.poll(cast(Any, self.bottle))
        follower.poll(cast(Any, self.bottle))
        self.assertEqual("one\ntwo\n", (self.copy_dir() / "s.jsonl").read_text())
        self.assertEqual("ONE\ntwo\nthree\n",
                         (self.copy_dir() / "s.rewrite-1.jsonl").read_text())
        self.audit.transcript_rewritten.assert_called_once_with(
            bottle="s1", activation="a1", copy="s1/a1/s.jsonl", new_copy="s1/a1/s.rewrite-1.jsonl",
            guest_path=str(path), offset=8, size=14)
        # The old copy closed with a final checkpoint of what it had.
        self.assertEqual([(8, True)], [(c["size"], c["final"]) for c in self.captured()])
        path.write_text("x\n")  # shrinks
        follower.poll(cast(Any, self.bottle))
        self.assertTrue((self.copy_dir() / "s.rewrite-2.jsonl").exists())

    def test_resumes_from_the_saved_offset(self) -> None:
        follower = self.begin()
        path = self.guest / "s.jsonl"
        path.write_text("a\n")
        follower.poll(cast(Any, self.bottle))
        # The host controller restarts: the old follower just stops.
        path.write_text("a\nb\n")
        resumed = TranscriptFollower.resume(session="s1", activation="a1", audit=self.audit)
        assert resumed is not None
        self.assertEqual("t-abc", resumed.agent_id)
        resumed.poll(cast(Any, self.bottle))
        self.assertEqual("a\nb\n", (self.copy_dir() / "s.jsonl").read_text())
        resumed.close(cast(Any, self.bottle))
        self.assertEqual(hashlib.sha256(b"a\nb\n").hexdigest(), self.captured()[-1]["sha256"])
        self.audit.transcript_rewritten.assert_not_called()

    def test_a_failed_poll_is_retried(self) -> None:
        follower = self.begin()
        (self.guest / "s.jsonl").write_text("a\n")
        self.bottle.fail = True
        follower.poll(cast(Any, self.bottle))
        self.bottle.fail = False
        bad = mock.Mock(exec=mock.Mock(return_value=ExecResult(1, "", "no sh")))
        follower.poll(bad)
        self.assertFalse((self.copy_dir() / "s.jsonl").exists())
        follower.poll(cast(Any, self.bottle))
        self.assertEqual("a\n", (self.copy_dir() / "s.jsonl").read_text())
        follower.poll()  # no bottle yet: nothing to do

    def test_close_stale_closes_only_ended_sessions(self) -> None:
        self.begin()
        (self.guest / "s.jsonl").write_text("a\n")
        follower = TranscriptFollower.resume(session="s1", activation="a1", audit=self.audit)
        assert follower is not None
        follower.poll(cast(Any, self.bottle))
        transcripts.close_stale({"s1"}, self.audit)
        self.assertEqual([], self.captured())
        transcripts.close_stale(set(), self.audit)
        self.assertEqual([(2, True)], [(c["size"], c["final"]) for c in self.captured()])


class TestBottleService(_Root):
    """The host controller follows each session it holds or re-adopts."""

    def setUp(self) -> None:
        super().setUp()
        from bot_bottle.control.hostctl.bottles import BottleService
        self.sessions = mock.MagicMock()
        self.sessions.records.return_value = []
        self.backend = mock.MagicMock()
        self.service = BottleService(self.sessions, backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None, audit=self.audit)
        self.pi_plan = mock.Mock(harness="pi", guest_home="/home/node")

    def follow(self, plan: Any, agent_id: str = "") -> None:
        from bot_bottle.contract.hostctl import LaunchRequest
        with mock.patch("bot_bottle.contrib.pi.transcript.transcript_glob",
                        return_value=self.glob):
            self.service._follow(  # pyright: ignore[reportPrivateUsage]
                plan, cast(Any, self.bottle), LaunchRequest(agent="pi", agent_id=agent_id),
                identity="s1", activation="a1")

    def followers(self) -> dict[str, TranscriptFollower]:
        return self.service._followers  # pyright: ignore[reportPrivateUsage]

    def test_a_held_pi_session_is_followed_and_closed(self) -> None:
        from bot_bottle.control.hostctl.remote import terminal_agent_id
        self.follow(self.pi_plan)
        follow = json.loads((self.copy_dir() / "follow.json").read_text())
        self.assertEqual(terminal_agent_id("s1", "a1"), follow["agent_id"])
        (self.guest / "s.jsonl").write_text("a\n")
        self.service._unfollow("s1", cast(Any, self.bottle))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual({}, self.followers())
        self.assertEqual([(2, True)], [(c["size"], c["final"]) for c in self.captured()])

    def test_a_console_launch_records_its_agent_id(self) -> None:
        self.follow(self.pi_plan, agent_id="c-42")
        self.assertEqual("c-42", json.loads((self.copy_dir() / "follow.json").read_text())
                         ["agent_id"])
        self.service._unfollow("s1")  # pyright: ignore[reportPrivateUsage]

    def test_a_harness_without_a_transcript_isnt_followed(self) -> None:
        self.follow(mock.Mock(harness="claude"))
        self.assertEqual({}, self.followers())

    def test_a_follower_that_cant_start_doesnt_stop_the_launch(self) -> None:
        self.bottle.fail = True
        self.follow(self.pi_plan)
        self.assertEqual({}, self.followers())
        self.assertIn("not copying its transcript", self.err.getvalue())

    def test_readoption_resumes_or_closes_the_copy(self) -> None:
        from bot_bottle.backend import adoption
        from bot_bottle.control.hostctl.sessions import SessionRecord
        self.follow(self.pi_plan)
        self.followers().pop("s1").close(cast(Any, self.bottle))
        record = SessionRecord(id="s1", kind="bottle", meta={"activation": "a1"})
        # Closed: nothing to resume.
        self.service._refollow(self.backend, record)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual({}, self.followers())
        self.follow(self.pi_plan)
        self.followers().pop("s1").detach()
        adoption.write("s1", adoption.AdoptionRecord(container="c", registry_id=""))
        self.backend.attach_adopted.return_value = self.bottle
        self.service._refollow(self.backend, record)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("s1", self.followers())
        # A restarted controller finishing it: final read, then closed.
        (self.guest / "s.jsonl").write_text("z\n")
        self.service._unfollow("s1", None, "a1")  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([(2, True)], [(c["size"], c["final"]) for c in self.captured()])

    def test_readoption_without_a_bottle_closes_the_copy(self) -> None:
        from bot_bottle.control.hostctl.sessions import SessionRecord
        self.follow(self.pi_plan)
        self.followers().pop("s1").detach()
        self.backend.attach_adopted.return_value = None
        record = SessionRecord(id="s1", kind="bottle", meta={"activation": "a1"})
        self.service._refollow(self.backend, record)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual({}, self.followers())
        self.assertIsNone(TranscriptFollower.resume(session="s1", activation="a1",
                                                    audit=self.audit))
        from bot_bottle.backend import adoption
        adoption.write("s1", adoption.AdoptionRecord(container="c", registry_id=""))
        self.backend.attach_adopted.side_effect = RuntimeError("boom")
        self.service._refollow(self.backend, record)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("couldn't resume copying", self.err.getvalue())

    def test_unfollow_never_raises(self) -> None:
        with mock.patch.object(TranscriptFollower, "resume", side_effect=OSError("disk")):
            self.service._unfollow("s1", None, "a1")  # pyright: ignore[reportPrivateUsage]
        self.assertIn("couldn't close its transcript copy", self.err.getvalue())

    def test_adopt_closes_copies_of_sessions_that_ended_meanwhile(self) -> None:
        self.follow(self.pi_plan)
        self.followers().pop("s1").detach()
        self.service.adopt()
        self.assertIsNone(TranscriptFollower.resume(session="s1", activation="a1",
                                                    audit=self.audit))


class TestBackendsAttach(unittest.TestCase):
    def test_each_backend_attaches_without_tearing_down(self) -> None:
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        from bot_bottle.backend.firecracker.backend import FirecrackerBottleBackend
        from bot_bottle.backend.macos_container.backend import MacosContainerBottleBackend
        from bot_bottle.backend.base import BottleBackend
        for cls in (DockerBottleBackend, FirecrackerBottleBackend, MacosContainerBottleBackend):
            module = cls.__module__.rsplit(".", 1)[0] + ".launch"
            with mock.patch(f"{module}.attach", return_value="handle") as attach:
                self.assertEqual("handle", cls.attach_adopted(cast(Any, None), "b1",
                                                              cast(Any, "rec")))
            attach.assert_called_once_with("b1", "rec")
        self.assertIsNone(BottleBackend.attach_adopted(cast(Any, None), "b1", cast(Any, "r")))


class TestPollScript(unittest.TestCase):
    def test_glob_quoting(self) -> None:
        self.assertEqual("'/a b/--x--/'*.jsonl", transcripts.shell_glob("/a b/--x--/*.jsonl"))

    def test_listing_parse(self) -> None:
        self.assertEqual([], transcripts.parse_listing("garbage\n"))
        [one, two] = transcripts.parse_listing("F 3 /p\nC YQ==\nD Yg==\nF 0 /q\n")
        self.assertEqual(("/p", 3, b"a", b"b"), (one.guest_path, one.size, one.check, one.data))
        self.assertEqual(("/q", None, b""), (two.guest_path, two.check, two.data))


class TestAuditEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.records: list[Any] = []
        self.audit = HostAudit(None)
        self.audit.record = self.records.append  # type: ignore[method-assign]

    def test_captured_is_agent_claimed_and_archives(self) -> None:
        self.audit.transcript_captured(bottle="b", activation="a", copy="b/a/s.jsonl",
                                       guest_path="/g/s.jsonl", size=5, sha256="ff", final=True)
        event = self.records[0]
        self.assertEqual("agent-claimed", event.payload["provenance"])
        self.assertEqual({"guest_path": "/g/s.jsonl"}, event.untrusted)
        ocsf = to_ocsf(event.to_json())
        self.assertEqual((1008, 4), (ocsf["class_uid"], ocsf["activity_id"]))

    def test_rewritten_is_a_finding(self) -> None:
        self.audit.transcript_rewritten(bottle="b", activation="a", copy="c", new_copy="n",
                                        guest_path="/g", offset=8, size=2)
        event = self.records[0]
        self.assertEqual(("security", 8, 2), (event.sensitivity, event.payload["copied_bytes"],
                                              event.payload["guest_bytes"]))
        self.assertEqual(2004, to_ocsf(event.to_json())["class_uid"])


if __name__ == "__main__":
    unittest.main()
