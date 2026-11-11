"""Unit: supervise queue error + edge branches (coverage ratchet,
ADR 0004). Complements test_supervise.py with the malformed-input and
fallback paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.contract import supervise
from bot_bottle.paths import bot_bottle_root, host_db_path
from bot_bottle.orchestrator.store.queue_store import QueueStore
from bot_bottle.contract.supervise import (
    Proposal,
    STATUS_APPROVED,
    TOOL_EGRESS_ALLOW,
)
from bot_bottle.orchestrator.supervisor import Supervisor

def _supervisor() -> Supervisor:
    """A `Supervisor` on the host DB under the current (test) root."""
    return Supervisor(host_db_path())


def _proposal() -> Proposal:
    return Proposal.new(
        bottle_slug="slug",
        tool=TOOL_EGRESS_ALLOW,
        proposed_file="x",
        justification="j",
    )


class TestPathHelpers(unittest.TestCase):
    def test_bot_bottle_root(self) -> None:
        self.assertTrue(str(bot_bottle_root()).endswith(".bot-bottle"))


class TestReadMalformed(unittest.TestCase):
    def test_read_proposal_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug", host_db_path()).migrate()
                with self.assertRaises(FileNotFoundError):
                    _supervisor().read_proposal("slug", "p")

    def test_read_response_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug", host_db_path()).migrate()
                with self.assertRaises(FileNotFoundError):
                    _supervisor().read_response("slug", "p")

    def test_list_pending_reads_db_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug", host_db_path()).migrate()
                _supervisor().write_proposal(_proposal())
                pending = _supervisor().list_pending_proposals("slug")
            self.assertEqual(1, len(pending))
            self.assertEqual("slug", pending[0].bottle_slug)

    def test_list_pending_skips_when_response_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"HOME": d}):
                QueueStore("slug", host_db_path()).migrate()
                p = _proposal()
                _supervisor().write_proposal(p)
                _supervisor().write_response("slug", supervise.Response(
                    proposal_id=p.id,
                    status=STATUS_APPROVED,
                    notes="",
                ))
                self.assertEqual([], _supervisor().list_pending_proposals("slug"))


class TestStoreGuardBranches(unittest.TestCase):
    """Direct QueueStore construction and early-return guard branches."""

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

    def test_queue_store_chmod_oserror_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "q.db"
            store = QueueStore("key", db_path=db)
            with patch("os.fchmod", side_effect=OSError("ro")):
                with self.assertRaisesRegex(OSError, "ro"):
                    store.migrate()


if __name__ == "__main__":
    unittest.main()
