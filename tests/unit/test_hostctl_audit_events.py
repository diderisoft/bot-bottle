"""Unit: what the host controller records, and that auditing never blocks it."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from bot_bottle.control.hostctl.audit_events import HostAudit, digest_manifest, digest_request
from bot_bottle.control.hostctl.audit_journal import AuditJournal, AuditJournalBusy


class HostAuditTest(unittest.TestCase):
    def test_a_journal_that_will_not_open_turns_recording_off(self) -> None:
        err = io.StringIO()
        with mock.patch("bot_bottle.control.hostctl.audit_events.AuditJournal",
                        side_effect=AuditJournalBusy("held")), redirect_stderr(err):
            audit = HostAudit.open()
            audit.rejected("GET /x", role=None, request_digest="d")  # no-op, no raise
        self.assertIn("not recording", err.getvalue())

    def test_a_failed_write_is_logged_not_raised(self) -> None:
        journal = mock.Mock(spec=AuditJournal)
        journal.append.side_effect = OSError("disk full")
        err = io.StringIO()
        with redirect_stderr(err):
            HostAudit(journal).rejected("GET /x", role="gateway", request_digest="d")
        self.assertIn("disk full", err.getvalue())

    def test_records_carry_the_hostctl_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = AuditJournal(Path(tmp), host="h", engine="e")
            try:
                HostAudit(journal).rejected("GET /x", role="gateway", request_digest="d")
                last = journal.path.read_text().splitlines()[-1]
            finally:
                journal.close()
        self.assertIn('"producer":"hostctl"', last)
        self.assertIn('"type":"hostctl.broker_reject"', last)


class Digests(unittest.TestCase):
    def test_a_request_digest_leaves_out_secret_values(self) -> None:
        a = digest_request({"agent": "demo", "secrets": {"TOKEN": "one"}})
        b = digest_request({"agent": "demo", "secrets": {"TOKEN": "two"}})
        c = digest_request({"agent": "demo", "secrets": {"OTHER": "one"}})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_an_unrenderable_manifest_digests_to_empty(self) -> None:
        err = io.StringIO()
        with mock.patch("bot_bottle.manifest.preview.manifest_to_yaml",
                        side_effect=AttributeError("no agent")), redirect_stderr(err):
            self.assertEqual("", digest_manifest(mock.Mock()))
        self.assertIn("couldn't digest", err.getvalue())


if __name__ == "__main__":
    unittest.main()
