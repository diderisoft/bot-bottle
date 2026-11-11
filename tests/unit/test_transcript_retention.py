"""Unit: ended sessions' transcript copies are deleted after the host's
retention, with their digests in the journal (PRD 0095)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli.commands import audit as audit_cmd
from bot_bottle.control.hostctl import transcript_retention
from bot_bottle.control.hostctl.audit_events import HostAudit
from bot_bottle.control.hostctl.audit_ocsf import to_ocsf
from bot_bottle.control.hostctl.transcripts import TranscriptStore, transcripts_root
from bot_bottle.lib.config_store import DEFAULT_TRANSCRIPT_RETENTION_DAYS, ConfigStore
from bot_bottle.paths import host_db_path
from . import serve_host_controller, use_bottle_root

_DAY = 86400.0
_NOW = 1_800_000_000.0


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbret.")))
        self.addCleanup(use_bottle_root(self.root))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.audit = mock.MagicMock(spec=HostAudit)

    def run_dir(self, session: str, activation: str, *, closed: bool, age_days: float,
                data: bytes = b"line\n") -> Path:
        run = transcripts_root() / session / activation
        run.mkdir(parents=True)
        (run / "s.jsonl").write_bytes(data)
        store = TranscriptStore(host_db_path())
        store.put_run(session, activation, glob="", skip=[], start={}, agent_id="c-1",
                      harness="pi", closed=closed)
        store.put_file(session, activation, "s.jsonl", guest_path="/g/s.jsonl", offset=5,
                       start=0, opened=1.0, agent_id="c-1", closed=closed)
        when = _NOW - age_days * _DAY
        for p in run.iterdir():
            os.utime(p, (when, when))
        return run


class TestSweep(_Root):
    def test_deletes_only_closed_runs_past_the_cutoff(self) -> None:
        old = self.run_dir("s1", "a1", closed=True, age_days=31)
        recent = self.run_dir("s1", "a2", closed=True, age_days=29)
        running = self.run_dir("s2", "a1", closed=False, age_days=90)
        gone = transcript_retention.sweep(self.audit, 30, now=_NOW)
        self.assertEqual([old], gone)
        self.assertFalse(old.exists())
        store = TranscriptStore(host_db_path())
        self.assertIsNone(store.run("s1", "a1"))  # its rows go with it
        self.assertEqual([], store.files("s1", "a1"))
        self.assertIsNotNone(store.run("s1", "a2"))
        self.assertTrue(recent.exists() and running.exists())
        self.audit.transcript_deleted.assert_called_once_with(
            bottle="s1", activation="a1", retention_days=30,
            copies=[{"copy": "s1/a1/s.jsonl", "bytes": 5,
                     "sha256": hashlib.sha256(b"line\n").hexdigest()}])

    def test_an_emptied_session_dir_goes_too(self) -> None:
        run = self.run_dir("s3", "a1", closed=True, age_days=40)
        transcript_retention.sweep(self.audit, 30, now=_NOW)
        self.assertFalse(run.parent.exists())

    def test_no_root_or_a_run_not_recorded(self) -> None:
        transcript_retention.sweep(self.audit, 30, now=_NOW)  # no root yet
        stray = transcripts_root() / "s4" / "a1"
        stray.mkdir(parents=True)
        self.assertEqual([], transcript_retention.sweep(self.audit, 30, now=_NOW))

    def test_a_row_naming_a_path_outside_the_root_is_never_deleted(self) -> None:
        outside = self.root / "keep"
        outside.mkdir()
        with TranscriptStore(host_db_path())._connection() as conn:  # pylint: disable=protected-access
            conn.execute("INSERT INTO transcript_runs VALUES (?, ?, '', '[]', '{}', '', '', 1)",
                         ("..", "keep"))
        self.assertEqual([], transcript_retention.sweep(self.audit, 0, now=_NOW + 1e9))
        self.assertTrue(outside.exists())
        self.assertIn("not sweeping", self.err.getvalue())

    def test_ids_are_checked_when_written(self) -> None:
        store = TranscriptStore(host_db_path())
        for session, activation in (("..", "a"), ("s", "../x"), ("", "a"), ("s", ".")):
            with self.subTest(session=session, activation=activation), \
                    self.assertRaises(ValueError):
                store.put_run(session, activation, glob="", skip=[], start={}, agent_id="",
                              harness="", closed=False)
        with self.assertRaises(ValueError):
            store.put_file("s", "a", "../s.jsonl", guest_path="", offset=0, start=0,
                           opened=0.0, agent_id="", closed=False)

    def test_a_failed_delete_is_reported_and_skipped(self) -> None:
        self.run_dir("s5", "a1", closed=True, age_days=40)
        with mock.patch.object(transcript_retention.shutil, "rmtree",
                               side_effect=OSError("busy")):
            self.assertEqual([], transcript_retention.sweep(self.audit, 30, now=_NOW))
        self.assertIn("couldn't delete", self.err.getvalue())
        self.audit.transcript_deleted.assert_not_called()


class TestSweeper(_Root):
    def test_sweeps_at_start_and_survives_a_failure(self) -> None:
        swept = threading.Event()
        calls: list[int] = []

        def days() -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db locked")
            swept.set()
            return 30

        sweeper = transcript_retention.RetentionSweeper(self.audit, retention_days=days,
                                                        interval=0.01)
        sweeper.start()
        self.assertTrue(swept.wait(5))
        sweeper.stop()
        self.assertIn("retention sweep failed", self.err.getvalue())

    def test_the_default_reads_the_host_setting(self) -> None:
        self.assertEqual(DEFAULT_TRANSCRIPT_RETENTION_DAYS,
                         transcript_retention._retention_days())  # pylint: disable=protected-access


class TestSetting(_Root):
    def setUp(self) -> None:
        super().setUp()
        serve_host_controller(self)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit_cmd.cmd_audit(["transcript-retention", *argv])
        return code, out.getvalue()

    def test_show_and_set(self) -> None:
        self.assertEqual((0, "30 days\n"), self.run_cli())
        self.assertEqual((0, "7 days\n"), self.run_cli("7"))
        self.assertEqual(7, ConfigStore(host_db_path()).transcript_retention_days())
        self.assertEqual(2, self.run_cli("0")[0])

    def test_an_unmigrated_db_reads_the_default(self) -> None:
        path = host_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        self.assertEqual(30, ConfigStore(path).transcript_retention_days())


class TestDeletedEvent(unittest.TestCase):
    def test_records_the_digests_and_maps_to_a_log_delete(self) -> None:
        records: list[Any] = []
        audit = HostAudit(None)
        audit.record = records.append  # type: ignore[method-assign]
        audit.transcript_deleted(bottle="b", activation="a", retention_days=30,
                                 copies=[{"copy": "b/a/s.jsonl", "bytes": 1, "sha256": "ff"}])
        event = records[0]
        self.assertEqual("transcript.deleted", event.type)
        ocsf = to_ocsf(event.to_json())
        self.assertEqual((1008, 2), (ocsf["class_uid"], ocsf["activity_id"]))


if __name__ == "__main__":
    unittest.main()
