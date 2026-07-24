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
from bot_bottle.store.audit_store import AuditStore
from bot_bottle.paths import bot_bottle_root
from bot_bottle.store.queue_store import QueueStore
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
        self.assertTrue(str(bot_bottle_root()).endswith(".bot-bottle"))


class TestReadMalformed(unittest.TestCase):
    def test_read_proposal_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                with self.assertRaises(FileNotFoundError):
                    read_proposal("slug", "p")

    def test_read_response_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                with self.assertRaises(FileNotFoundError):
                    read_response("slug", "p")

    def test_list_pending_reads_db_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                supervise.write_proposal(_proposal())
                pending = list_pending_proposals("slug")
            self.assertEqual(1, len(pending))
            self.assertEqual("slug", pending[0].bottle_slug)

    def test_list_pending_skips_when_response_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                p = _proposal()
                supervise.write_proposal(p)
                supervise.write_response("slug", supervise.Response(
                    proposal_id=p.id,
                    status=STATUS_APPROVED,
                    notes="",
                ))
                self.assertEqual([], list_pending_proposals("slug"))


class TestWaitForResponse(unittest.TestCase):
    def test_missing_response_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                with self.assertRaises(TimeoutError):
                    wait_for_response("slug", "p", deadline=time.monotonic())

    def test_empty_db_response_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug").migrate()
                with self.assertRaises(TimeoutError):
                    wait_for_response("slug", "p", deadline=time.monotonic())


class TestReadAuditEntries(unittest.TestCase):
    def test_missing_log_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            self.assertEqual([], read_audit_entries("egress", "nope"))

    def test_reads_entries_from_db(self) -> None:
        with tempfile.TemporaryDirectory() as home, \
                patch.dict("os.environ", {"HOME": home}):
            AuditStore().migrate()
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


class TestStoreGuardBranches(unittest.TestCase):
    """Direct QueueStore / AuditStore construction and early-return guard branches."""

    def test_queue_store_explicit_db_path(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            store.migrate()
            self.assertTrue(db.is_file())
            self.assertEqual(db, store.db_path)

    def test_queue_store_missing_db_list_pending_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            store.migrate()
            db.unlink()
            self.assertEqual([], store.list_pending_proposals())

    def test_queue_store_missing_db_list_all_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            store.migrate()
            db.unlink()
            self.assertEqual([], store.list_all_pending_proposals())

    def test_queue_store_missing_db_archive_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            store.migrate()
            db.unlink()
            store.archive_proposal("anything")  # must not raise

    def test_queue_store_chmod_oserror_is_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            with patch("pathlib.Path.chmod", side_effect=OSError("ro")):
                store.migrate()  # must not raise

    def test_audit_store_missing_db_read_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "a.db"
            store = AuditStore(db_path=db)
            store.migrate()
            db.unlink()
            self.assertEqual([], store.read_audit_entries("egress", "slug"))

    def test_audit_store_chmod_oserror_is_swallowed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "a.db"
            store = AuditStore(db_path=db)
            with patch("pathlib.Path.chmod", side_effect=OSError("ro")):
                store.migrate()  # must not raise


if __name__ == "__main__":
    unittest.main()
