"""Unit: canary event store (issue #997)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.unit import use_bottle_root
from bot_bottle.paths import host_db_path
from bot_bottle.contract.supervise import (
    CANARY_STATUS_ACKNOWLEDGED,
    CANARY_STATUS_TERMINATED,
    CANARY_STATUS_UNRESOLVED,
    CanaryEvent,
)
from bot_bottle.orchestrator.store.canary_store import CanaryStore
from bot_bottle.orchestrator.supervisor import Supervisor


FIXED_TS = datetime(2026, 11, 5, 16, 0, 0, tzinfo=timezone.utc)
RESOLVED_TS = datetime(2026, 11, 5, 17, 0, 0, tzinfo=timezone.utc).isoformat()


def _event(
    bottle_slug: str = "dev",
    reason: str = "canary token detected",
    request_path: str = "api.example.com/v1/completions",
) -> CanaryEvent:
    return CanaryEvent.new(
        bottle_slug=bottle_slug,
        reason=reason,
        request_path=request_path,
        now=FIXED_TS,
    )


def _supervisor() -> Supervisor:
    return Supervisor(host_db_path())


class TestCanaryEventRoundtrip(unittest.TestCase):
    def test_new_stamps_uuid_and_timestamp(self):
        e = _event()
        self.assertTrue(e.id)
        self.assertEqual("2026-11-05T16:00:00+00:00", e.arrival_timestamp)
        self.assertEqual("dev", e.bottle_slug)
        self.assertEqual(CANARY_STATUS_UNRESOLVED, e.status)
        self.assertEqual("", e.resolved_at)

    def test_to_from_dict_roundtrip(self):
        e = _event()
        self.assertEqual(e, CanaryEvent.from_dict(e.to_dict()))

    def test_from_dict_rejects_unknown_status(self):
        raw = _event().to_dict()
        raw["status"] = "maybe"
        with self.assertRaises(ValueError):
            CanaryEvent.from_dict(raw)


class TestCanaryStoreIO(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-canary-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        CanaryStore(host_db_path()).migrate()

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def _store(self) -> CanaryStore:
        return CanaryStore(host_db_path())

    def test_write_and_read_roundtrip(self):
        e = _event()
        self._store().write_event(e)
        got = self._store().read_event(e.id)
        self.assertEqual(e, got)

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._store().read_event("no-such-id")

    def test_list_unresolved_empty_when_no_db(self):
        import shutil
        shutil.rmtree(self._tmp.name, ignore_errors=True)
        Path(self._tmp.name).mkdir()
        self.assertEqual([], CanaryStore(host_db_path()).list_unresolved())

    def test_list_unresolved_returns_unresolved_only(self):
        e1 = _event(bottle_slug="a")
        e2 = _event(bottle_slug="b")
        s = self._store()
        s.write_event(e1)
        s.write_event(e2)
        s.resolve_event(e1.id, status=CANARY_STATUS_ACKNOWLEDGED,
                        resolved_at=RESOLVED_TS, notes="fine")
        unresolved = self._store().list_unresolved()
        self.assertEqual(1, len(unresolved))
        self.assertEqual(e2.id, unresolved[0].id)

    def test_resolve_event_acknowledge(self):
        e = _event()
        s = self._store()
        s.write_event(e)
        ok = s.resolve_event(e.id, status=CANARY_STATUS_ACKNOWLEDGED,
                             resolved_at=RESOLVED_TS, notes="all good")
        self.assertTrue(ok)
        got = s.read_event(e.id)
        self.assertEqual(CANARY_STATUS_ACKNOWLEDGED, got.status)
        self.assertEqual(RESOLVED_TS, got.resolved_at)
        self.assertEqual("all good", got.resolution_notes)

    def test_resolve_event_terminate(self):
        e = _event()
        s = self._store()
        s.write_event(e)
        ok = s.resolve_event(e.id, status=CANARY_STATUS_TERMINATED,
                             resolved_at=RESOLVED_TS, notes="shutting down")
        self.assertTrue(ok)
        self.assertEqual(CANARY_STATUS_TERMINATED, s.read_event(e.id).status)

    def test_resolve_already_resolved_returns_false(self):
        e = _event()
        s = self._store()
        s.write_event(e)
        s.resolve_event(e.id, status=CANARY_STATUS_ACKNOWLEDGED,
                        resolved_at=RESOLVED_TS, notes="")
        # second resolve returns False
        ok = s.resolve_event(e.id, status=CANARY_STATUS_TERMINATED,
                             resolved_at=RESOLVED_TS, notes="")
        self.assertFalse(ok)
        # status is still acknowledged (first stands)
        self.assertEqual(CANARY_STATUS_ACKNOWLEDGED, s.read_event(e.id).status)

    def test_delete_all_for_bottle(self):
        e1 = _event(bottle_slug="dev")
        e2 = _event(bottle_slug="dev")
        e3 = _event(bottle_slug="other")
        s = self._store()
        for e in (e1, e2, e3):
            s.write_event(e)
        s.delete_all_for_bottle("dev")
        # dev's events are gone, not kept: the journal is their record
        remaining = s.list_unresolved()
        self.assertEqual(1, len(remaining))
        self.assertEqual("other", remaining[0].bottle_slug)
        with sqlite3.connect(s.db_path) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM canary_events").fetchone()[0])

    def test_delete_idempotent_on_missing_db(self):
        # Should not raise when no DB exists
        CanaryStore(host_db_path()).delete_all_for_bottle("dev")


class TestSupervisorCanaryDelegation(unittest.TestCase):
    """Supervisor delegates canary methods to CanaryStore."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bot-bottle-sv-canary-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        CanaryStore(host_db_path()).migrate()

    def tearDown(self):
        self._restore()
        self._tmp.cleanup()

    def test_write_and_list_unresolved(self):
        sv = _supervisor()
        e = _event()
        sv.write_canary_event(e)
        unresolved = sv.list_unresolved_canary_events()
        self.assertEqual(1, len(unresolved))
        self.assertEqual(e.id, unresolved[0].id)

    def test_resolve_canary_event(self):
        sv = _supervisor()
        e = _event()
        sv.write_canary_event(e)
        ok = sv.resolve_canary_event(
            e.id, status=CANARY_STATUS_ACKNOWLEDGED,
            resolved_at=RESOLVED_TS, notes="ok",
        )
        self.assertTrue(ok)
        self.assertEqual([], sv.list_unresolved_canary_events())

    def test_delete_canary_events(self):
        sv = _supervisor()
        e = _event(bottle_slug="dev")
        sv.write_canary_event(e)
        sv.delete_canary_events("dev")
        self.assertEqual([], sv.list_unresolved_canary_events())


if __name__ == "__main__":
    unittest.main()
