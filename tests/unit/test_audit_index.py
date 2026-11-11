"""Unit: the audit journal's search index."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.control.hostctl.audit_index import AuditIndex, IndexBreak, Query
from bot_bottle.control.hostctl.audit_journal import AuditJournal


def _event(**overrides: Any) -> AuditEvent:
    base: dict[str, Any] = {
        "type": "egress.decision", "actor": "bottled-agent:a", "action": "egress.connect",
        "outcome": "blocked", "sensitivity": "security",
        "ts_event": "2026-07-26T18:00:00.000Z", "bottle": "amber-fox"}
    base.update(overrides)
    return AuditEvent(**base)


class Index(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "audit"
        self.db = Path(tmp.name) / "db" / "index.db"
        self.journal = AuditJournal(self.dir, host="h", engine="e")

    def append(self, **overrides: Any) -> dict[str, Any]:
        record = self.journal.append(_event(**overrides), producer="egress")
        assert record is not None
        return record

    def ids(self, **filters: Any) -> list[str]:
        index = AuditIndex(self.dir, self.db)
        index.sync()
        return [r["id"] for r in index.query(Query(**filters))]

    def test_sync_indexes_only_new_records(self) -> None:
        index = AuditIndex(self.dir, self.db)
        self.assertEqual(index.sync(), 1)  # the segment_open record
        self.append()
        self.append()
        self.assertEqual(index.sync(), 2)
        self.assertEqual(index.sync(), 0)

    def test_rows_hold_the_journal_record_verbatim(self) -> None:
        record = self.append(payload={"route_id": 4})
        index = AuditIndex(self.dir, self.db)
        self.assertEqual(index.query(Query(type="egress.decision")), [])  # until synced
        index.sync()
        self.assertEqual(index.query(Query(type="egress.decision")), [record])

    def test_filters(self) -> None:
        a = self.append(outcome="allowed")["id"]
        b = self.append(bottle="other", type="forge.push_rejected",
                        payload={"repository": "o/r", "commit_sha": "abc"})["id"]
        c = self.append(ts_event="2026-07-27T00:00:00.000Z")["id"]
        self.assertEqual(self.ids(outcome="allowed"), [a])
        self.assertEqual(self.ids(bottle="other"), [b])
        self.assertEqual(self.ids(type="forge.*"), [b])
        self.assertEqual(self.ids(repository="o/r", commit_sha="abc"), [b])
        self.assertEqual(self.ids(type="egress.*", since="2026-07-27"), [c])
        self.assertEqual(self.ids(type="egress.*", until="2026-07-27"), [a])

    def test_a_type_glob_is_not_a_like_pattern(self) -> None:
        self.append(type="egressxdecision.x")
        self.assertEqual(self.ids(type="egress_*"), [])

    def test_sync_stops_at_a_record_that_does_not_chain(self) -> None:
        index = AuditIndex(self.dir, self.db)
        index.sync()
        self.append()
        lines = self.journal.path.read_text().splitlines()
        record = json.loads(lines[-1])
        record["outcome"] = "allowed"
        lines[-1] = json.dumps(record)
        self.journal.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(IndexBreak):
            index.sync()

    def test_a_torn_tail_is_left_for_the_next_sync(self) -> None:
        index = AuditIndex(self.dir, self.db)
        with self.journal.path.open("a") as f:
            f.write('{"id":"half')
        self.assertEqual(index.sync(), 1)

    def test_a_long_sync_keeps_what_it_committed_before_a_break(self) -> None:
        for _ in range(4):
            self.append()
        lines = self.journal.path.read_text().splitlines()
        record = json.loads(lines[-1])
        record["outcome"] = "allowed"
        lines[-1] = json.dumps(record)
        self.journal.path.write_text("\n".join(lines) + "\n")
        index = AuditIndex(self.dir, self.db)
        with patch("bot_bottle.control.hostctl.audit_index.SYNC_COMMIT_RECORDS", 2), \
                self.assertRaises(IndexBreak):
            index.sync()
        self.assertEqual(len(index.query(Query())), 4)  # the segment_open record and 3

    def test_rebuild_replays_the_journal(self) -> None:
        self.append()
        index = AuditIndex(self.dir, self.db)
        index.sync()
        with index._connection() as conn:  # pylint: disable=protected-access
            conn.execute("UPDATE audit_events SET outcome = 'allowed'")
        self.assertEqual(index.rebuild(), 2)
        self.assertEqual(len(index.query(Query(outcome="blocked"))), 1)

    def test_rebuild_refuses_a_broken_journal(self) -> None:
        self.append()
        lines = self.journal.path.read_text().splitlines()
        del lines[0]
        lines.insert(1, lines[0])  # a duplicated record breaks the chain
        self.journal.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(IndexBreak):
            AuditIndex(self.dir, self.db).rebuild()

    def test_the_index_lives_in_the_host_db(self) -> None:
        index = AuditIndex(self.dir, self.db)
        index.sync()
        self.assertEqual(index.db_path, self.db)
        self.assertEqual(list(self.dir.glob("*.db")), [])


if __name__ == "__main__":
    unittest.main()
