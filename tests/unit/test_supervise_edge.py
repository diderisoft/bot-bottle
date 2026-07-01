"""Unit: supervise queue/audit error + edge branches (coverage ratchet,
ADR 0004). Complements test_supervise.py with the malformed-input and
fallback paths."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle import supervise
from bot_bottle.supervise import (
    AuditEntry,
    Proposal,
    STATUS_APPROVED,
    TOOL_EGRESS_ALLOW,
    list_pending_proposals,
    read_audit_entries,
    read_proposal,
    read_response,
    wait_for_response,
    write_audit_entry,
)


def _proposal() -> Proposal:
    return Proposal.new(
        bottle_slug="slug",
        tool=TOOL_EGRESS_ALLOW,
        proposed_file="x",
        justification="j",
        current_file_hash="h",
    )


class TestPathHelpers(unittest.TestCase):
    def test_bot_bottle_root(self) -> None:
        self.assertTrue(str(supervise.bot_bottle_root()).endswith(".bot-bottle"))

    def test_queue_dir_for_slug(self) -> None:
        self.assertIn("slug", str(supervise.queue_dir_for_slug("slug")))

    def test_queue_db_path_for_slug_dir(self) -> None:
        self.assertEqual(
            supervise.host_db_path(),
            supervise.queue_db_path(Path("/tmp/queue")),
        )


class TestReadMalformed(unittest.TestCase):
    def test_read_proposal_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                read_proposal(Path(d), "p")

    def test_read_response_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                read_response(Path(d), "p")

    def test_list_pending_ignores_legacy_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qd = Path(d)
            (qd / "bad.proposal.json").write_text("{ not json")
            (qd / "arr.proposal.json").write_text("[]")
            supervise.write_proposal(qd, _proposal())  # one valid
            pending = list_pending_proposals(qd)
            self.assertEqual(1, len(pending))
            self.assertEqual("slug", pending[0].bottle_slug)

    def test_list_pending_skips_when_response_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qd = Path(d)
            p = _proposal()
            supervise.write_proposal(qd, p)
            supervise.write_response(qd, supervise.Response(
                proposal_id=p.id,
                status=STATUS_APPROVED,
                notes="",
            ))
            self.assertEqual([], list_pending_proposals(qd))


class TestWaitForResponse(unittest.TestCase):
    def test_missing_response_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TimeoutError):
                wait_for_response(Path(d), "p", deadline=time.monotonic())

    def test_legacy_response_file_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "p.response.json").write_text("{}")  # dict but from_dict raises
            with self.assertRaises(TimeoutError):
                wait_for_response(Path(d), "p", deadline=time.monotonic())


class TestReadAuditEntries(unittest.TestCase):
    def test_missing_log_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            self.assertEqual([], read_audit_entries("egress", "nope"))

    def test_reads_entries_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            write_audit_entry(AuditEntry(
                timestamp="t",
                bottle_slug="slug",
                component="egress",
                operator_action="approve",
                operator_notes="",
                justification="",
                diff="",
            ))
            write_audit_entry(AuditEntry(
                timestamp="t",
                bottle_slug="other",
                component="egress",
                operator_action="reject",
                operator_notes="",
                justification="",
                diff="",
            ))
            entries = read_audit_entries("egress", "slug")
            self.assertEqual(1, len(entries))
            self.assertEqual("approve", entries[0].operator_action)

    def test_legacy_audit_log_file_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            path = supervise.audit_log_path("egress", "slug")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"timestamp": "t", "bottle_slug": "slug", "component": "egress",'
                ' "operator_action": "approve", "operator_notes": "",'
                ' "justification": "", "diff": ""}\n'
            )
            entries = read_audit_entries("egress", "slug")
            self.assertEqual([], entries)


if __name__ == "__main__":
    unittest.main()
