"""Unit: the audit journal across segments: rotation by size and by day,
retention and `journal.pruned`, and every reader of more than one segment."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract.audit import AuditEvent, record_hash
from bot_bottle.control.hostctl import audit_retention
from bot_bottle.control.hostctl.audit_events import HostAudit
from bot_bottle.control.hostctl.audit_forward import Cursor, read_batch
from bot_bottle.control.hostctl.audit_import import import_segment
from bot_bottle.control.hostctl.audit_index import AuditIndex, Query, read_chain
from bot_bottle.control.hostctl.audit_journal import (
    PRUNED, AuditJournal, last_record, segment_start, segments,
)
from bot_bottle.control.hostctl.audit_verify import verify
from bot_bottle.lib.config_store import AuditRetention

DAY = 86400.0
# 2026-07-25T10:00:00Z
T0 = 1_784_973_600.0


def _event(**overrides: Any) -> AuditEvent:
    base: dict[str, Any] = {
        "type": "egress.decision", "actor": "bottled-agent:a", "action": "egress.connect",
        "outcome": "blocked", "sensitivity": "security",
        "ts_event": "2026-07-25T10:00:00.000Z", "bottle": "amber-fox"}
    base.update(overrides)
    return AuditEvent(**base)


def _records(directory: Path) -> list[dict[str, Any]]:
    return [r for r, _ in read_chain(directory)]


def _close_quietly(j: AuditJournal) -> None:
    try:
        j.close()
    except OSError:
        pass  # already closed by the test


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "audit"
        self.db = self.root / "db" / "index.db"
        self.now = T0

    def journal(self, max_segment_bytes: int = 1 << 20) -> AuditJournal:
        j = AuditJournal(self.dir, host="h", engine="e", clock=lambda: self.now,
                         max_segment_bytes=max_segment_bytes)
        self.addCleanup(_close_quietly, j)
        return j

    def days_of_segments(self, journal: AuditJournal, days: int, per_day: int = 2) -> None:
        """`days` daily segments, `per_day` events each, ending today."""
        for _ in range(days):
            for _ in range(per_day):
                journal.append(_event(), producer="egress")
            self.now += DAY


class Rotation(_Case):
    def test_a_full_segment_starts_a_new_one(self) -> None:
        j = self.journal(max_segment_bytes=2000)
        first = j.path
        for _ in range(12):
            j.append(_event(), producer="egress")
            self.now += 1
        self.assertGreater(len(segments(self.dir)), 2)
        self.assertNotEqual(first, j.path)
        for path in segments(self.dir)[:-1]:
            # Closed once it reached the limit: never more than one record over.
            self.assertLess(path.stat().st_size, 2000 + 1000)
        opens = [r for r in _records(self.dir) if r["type"] == "audit.segment_open"]
        self.assertEqual(first.stem, opens[1]["payload"]["previous"])
        report = verify(self.dir, anchored=True)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(len(segments(self.dir)), report.segments)

    def test_each_record_is_in_the_segment_it_names(self) -> None:
        j = self.journal(max_segment_bytes=1500)
        for _ in range(8):
            j.append(_event(), producer="egress")
        for path in segments(self.dir):
            for line in path.read_text().splitlines():
                self.assertEqual(path.stem, json.loads(line)["segment"])

    def test_a_new_day_starts_a_new_segment(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        self.now += 3600
        j.append(_event(), producer="egress")
        self.assertEqual(1, len(segments(self.dir)))
        self.now += DAY
        record = j.append(_event(), producer="egress")
        assert record is not None
        self.assertEqual(2, len(segments(self.dir)))
        self.assertEqual("20260726", record["segment"][:8])
        self.assertTrue(verify(self.dir, anchored=True).ok)

    def test_a_restart_on_a_later_day_rotates_on_the_next_record(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        j.close()
        self.now += DAY
        j = self.journal()
        self.assertEqual(1, len(segments(self.dir)))  # an existing segment is adopted
        j.append(_event(), producer="egress")
        self.assertEqual(2, len(segments(self.dir)))
        self.assertTrue(verify(self.dir, anchored=True).ok)

    def test_segments_opened_in_one_second_still_sort_in_order(self) -> None:
        j = self.journal(max_segment_bytes=1)  # every record fills its segment
        for _ in range(4):
            j.append(_event(), producer="egress")
        names = [p.name for p in segments(self.dir)]
        self.assertEqual(5, len(names))
        self.assertEqual(sorted(set(names)), names)
        self.assertTrue(verify(self.dir, anchored=True).ok)

    def test_segment_start_reads_the_name(self) -> None:
        self.assertEqual(T0, segment_start("20260725T100000Z.jsonl"))
        self.assertIsNone(segment_start("backup.jsonl"))

    def test_last_record_reads_back_from_the_end(self) -> None:
        j = self.journal()
        last = [j.append(_event(), producer="egress") for _ in range(30)][-1]
        self.assertEqual(last, last_record(j.path, chunk=64))
        with j.path.open("a") as f:
            f.write('{"torn')
        self.assertEqual(last, last_record(j.path, chunk=64))


class Pruning(_Case):
    def test_age_drops_whole_old_segments_and_records_it_first(self) -> None:
        j = self.journal()
        self.days_of_segments(j, 5)
        j.append(_event(), producer="egress")
        before = segments(self.dir)
        dropped_bytes = sum(p.stat().st_size for p in before[:3])
        dropped_last = last_record(before[2])
        assert dropped_last is not None
        pruned = j.prune(max_age_days=2, keep_from="~")
        assert pruned is not None
        after = segments(self.dir)
        self.assertEqual(before[3:], after)
        payload = pruned["payload"]
        self.assertEqual(PRUNED, pruned["type"])
        self.assertEqual([p.stem for p in before[:3]], payload["segments"])
        self.assertEqual(dropped_last["hash"], payload["last_hash"])
        self.assertEqual({"epoch": 0, "seq": 0}, {k: payload["first"][k]
                                                  for k in ("epoch", "seq")})
        self.assertEqual((dropped_last["epoch"], dropped_last["seq"],
                          dropped_last["ts_recorded"]),
                         (payload["last"]["epoch"], payload["last"]["seq"],
                          payload["last"]["ts"]))
        self.assertEqual(dropped_bytes, payload["bytes"])

    def test_verification_starts_from_the_pruned_record(self) -> None:
        j = self.journal()
        self.days_of_segments(j, 4)
        j.prune(max_age_days=1, keep_from="~")
        report = verify(self.dir, anchored=True)
        self.assertTrue(report.ok, report.problems)
        self.assertTrue(report.starts_after)
        pruned_segment, line = report.pruned_at.split(":")
        record = json.loads(
            (self.dir / pruned_segment).read_text().splitlines()[int(line) - 1])
        self.assertEqual(report.starts_after, record["payload"]["last_hash"])
        # Pruning again keeps the chain anchored on the newest prune.
        self.days_of_segments(j, 2)
        j.prune(max_age_days=1, keep_from="~")
        self.assertTrue(verify(self.dir, anchored=True).ok)

    def test_segments_deleted_without_a_prune_record_are_a_break(self) -> None:
        j = self.journal()
        self.days_of_segments(j, 3)
        segments(self.dir)[0].unlink()
        report = verify(self.dir, anchored=True)
        self.assertEqual(["unrecorded-prune"], [p.kind for p in report.breaks])
        # An imported chain may start anywhere.
        self.assertTrue(verify(self.dir).ok)

    def test_size_drops_the_oldest_until_it_fits(self) -> None:
        j = self.journal(max_segment_bytes=1500)
        for _ in range(20):
            j.append(_event(), producer="egress")
        total = sum(p.stat().st_size for p in segments(self.dir))
        pruned = j.prune(max_bytes=total // 2, keep_from="~")
        assert pruned is not None
        kept = segments(self.dir)
        # Over the limit only by what's been written since: the pruned record.
        kept_before_record = sum(p.stat().st_size for p in kept) - len(
            kept[-1].read_text().splitlines()[-1]) - 1
        self.assertLessEqual(kept_before_record, total // 2)
        self.assertTrue(verify(self.dir, anchored=True).ok)

    def test_the_current_segment_is_never_dropped(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        self.now += 400 * DAY
        self.assertIsNone(j.prune(max_age_days=1, max_bytes=1, keep_from="~"))
        self.assertEqual(1, len(segments(self.dir)))

    def test_nothing_from_keep_from_on_is_dropped(self) -> None:
        j = self.journal()
        self.days_of_segments(j, 5)
        names = [p.name for p in segments(self.dir)]
        j.prune(max_age_days=1, keep_from=names[1])
        self.assertEqual(names[1:], [p.name for p in segments(self.dir)][:len(names) - 1])
        self.assertIsNone(j.prune(max_age_days=1, keep_from=""))

    def test_no_limits_keep_everything(self) -> None:
        j = self.journal()
        self.days_of_segments(j, 3)
        self.assertIsNone(j.prune(keep_from="~"))
        self.assertEqual(3, len(segments(self.dir)))


class Readers(_Case):
    """Each reader of the journal across rotated and pruned segments."""

    def rotated(self) -> AuditJournal:
        j = self.journal(max_segment_bytes=1500)
        for i in range(10):
            j.append(_event(bottle=f"b{i}"), producer="egress")
        self.assertGreater(len(segments(self.dir)), 2)
        return j

    def test_the_index_reads_every_segment_and_survives_a_prune(self) -> None:
        j = self.rotated()
        index = AuditIndex(self.dir, self.db)
        self.assertEqual(len(_records(self.dir)), index.sync())
        self.assertEqual(segments(self.dir)[-1].name, index.cursor_segment())
        self.now += 3 * DAY
        j.append(_event(bottle="late"), producer="egress")
        j.prune(max_age_days=1, keep_from=index.cursor_segment())
        # The new day's segment_open, the late event and journal.pruned.
        self.assertEqual(3, index.sync())
        self.assertEqual(1, len(index.query(Query(type=PRUNED))))
        # A rebuild starts at the oldest kept segment.
        self.assertEqual(len(_records(self.dir)), index.rebuild())

    def test_forwarding_reads_every_segment_and_resumes_after_a_prune(self) -> None:
        j = self.rotated()
        lines, cursor = read_batch(self.dir, Cursor())
        self.assertEqual([json.dumps(r, separators=(",", ":"), sort_keys=True)
                          for r in _records(self.dir)],
                         [json.dumps(json.loads(line), separators=(",", ":"), sort_keys=True)
                          for line in lines])
        self.now += 3 * DAY
        j.append(_event(bottle="late"), producer="egress")
        j.prune(max_age_days=1, keep_from=cursor.segment)
        more, _ = read_batch(self.dir, cursor)
        self.assertEqual(["audit.segment_open", "egress.decision", PRUNED],
                         [json.loads(m)["type"] for m in more])

    def test_rotated_segments_import_one_by_one_as_one_chain(self) -> None:
        self.rotated()
        other = self.root / "other"
        other.mkdir()
        index = AuditIndex(other, self.root / "other.db")
        results = [import_segment(other, path, index) for path in segments(self.dir)]
        self.assertFalse(results[0].extended)
        self.assertTrue(all(r.extended for r in results[1:]))
        self.assertEqual({results[0].chain}, {r.chain for r in results})
        self.assertTrue(verify(results[0].chain).ok)

    def test_the_cli_says_where_a_pruned_chain_starts(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.commands import audit as audit_cmd
        j = self.journal()
        self.days_of_segments(j, 3)
        j.prune(max_age_days=1, keep_from="~")
        out = io.StringIO()
        with mock.patch.object(audit_cmd, "host_audit_dir", return_value=self.dir), \
                redirect_stdout(out):
            self.assertEqual(0, audit_cmd.cmd_audit(["verify"]))
        self.assertIn("older segments were pruned", out.getvalue())
        self.assertIn("local: ok", out.getvalue())
        segments(self.dir)[0].unlink()
        with mock.patch.object(audit_cmd, "host_audit_dir", return_value=self.dir), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(1, audit_cmd.cmd_audit(["verify"]))


class Retention(_Case):
    """The host controller's prune: the setting, gated on every reader."""

    def setUp(self) -> None:
        super().setUp()
        self.j = self.journal()
        self.audit = HostAudit(self.j)
        self.days_of_segments(self.j, 5)

    def test_keep_all_prunes_nothing(self) -> None:
        index = AuditIndex(self.dir, self.db)
        self.assertIsNone(audit_retention.prune(self.audit, AuditRetention(), index=index,
                                                forwarded=lambda: None))
        self.assertEqual(5, len(segments(self.dir)))

    def test_it_indexes_first_then_prunes_what_the_setting_allows(self) -> None:
        index = AuditIndex(self.dir, self.db)
        pruned = audit_retention.prune(self.audit, AuditRetention(days=2), index=index,
                                       forwarded=lambda: None)
        assert pruned is not None
        self.assertEqual(3, len(pruned["payload"]["segments"]))
        self.assertTrue(verify(self.dir, anchored=True).ok)
        # The dropped records stay searchable: they were indexed before.
        self.assertEqual(10, len(index.query(Query(type="egress.decision"))))

    def test_unsent_segments_wait_for_the_forwarder(self) -> None:
        index = AuditIndex(self.dir, self.db)
        second = segments(self.dir)[1].name
        pruned = audit_retention.prune(self.audit, AuditRetention(days=1), index=index,
                                       forwarded=lambda: second)
        assert pruned is not None
        self.assertEqual(1, len(pruned["payload"]["segments"]))
        self.assertIsNone(audit_retention.prune(self.audit, AuditRetention(days=1),
                                                index=index, forwarded=lambda: ""))

    def test_the_gate_reads_the_cursor_from_a_rebuilt_index_db(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import index_db
        index = AuditIndex(self.dir, self.db)
        index.sync()
        self.db.unlink()
        self.assertTrue(index_db.open_for_start(self.db))
        with mock.patch.object(index_db, "host_audit_dir", return_value=self.dir), \
                redirect_stderr(io.StringIO()):
            index_db.rebuild(self.db)
            fresh = index_db.audit_index(self.db)
        self.assertEqual(segments(self.dir)[-1].name, fresh.cursor_segment())
        pruned = audit_retention.prune(self.audit, AuditRetention(days=2), index=fresh,
                                       forwarded=lambda: None)
        assert pruned is not None
        self.assertEqual(3, len(pruned["payload"]["segments"]))

    def test_the_pruned_record_chains(self) -> None:
        index = AuditIndex(self.dir, self.db)
        pruned = audit_retention.prune(self.audit, AuditRetention(max_gb=0, days=1),
                                       index=index, forwarded=lambda: None)
        assert pruned is not None
        self.assertEqual(pruned["hash"], record_hash(pruned["prev"], pruned))


if __name__ == "__main__":
    unittest.main()
