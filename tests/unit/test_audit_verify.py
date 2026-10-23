"""Unit: the offline audit chain check."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bot_bottle.contract.audit import AuditEvent
from bot_bottle.control.hostctl.audit_journal import AuditJournal
from bot_bottle.control.hostctl.audit_verify import verify


def _event() -> AuditEvent:
    return AuditEvent(type="egress.decision", actor="bottled-agent:a", action="egress.connect",
                      outcome="blocked", sensitivity="security",
                      ts_event="2026-07-26T18:22:04.061Z")


class Verify(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        journal = AuditJournal(self.dir, host="h", engine="e")
        for _ in range(4):
            journal.append(_event(), producer="egress")
        self.path = journal.path

    def lines(self) -> list[str]:
        return self.path.read_text().splitlines()

    def write(self, lines: list[str], path: Path | None = None) -> None:
        (path or self.path).write_text("".join(line + "\n" for line in lines))

    def kinds(self) -> list[str]:
        return [p.kind for p in verify(self.dir).problems]

    def test_an_untouched_journal_verifies(self) -> None:
        report = verify(self.dir)
        self.assertTrue(report.ok)
        self.assertEqual((report.segments, report.records), (1, 5))
        self.assertEqual(report.head, json.loads(self.lines()[-1])["hash"])
        self.assertEqual(report.starts_after, "")

    def test_an_edited_record_is_named(self) -> None:
        lines = self.lines()
        record = json.loads(lines[2])
        record["outcome"] = "allowed"
        lines[2] = json.dumps(record)
        self.write(lines)
        report = verify(self.dir)
        self.assertFalse(report.ok)
        [problem] = report.problems  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual((problem.kind, problem.line), ("edited", 3))

    def test_an_edit_that_rehashes_breaks_the_next_link(self) -> None:
        """Recomputing the edited record's hash moves the break, not hides it."""
        from bot_bottle.contract.audit import record_hash  # pylint: disable=import-outside-toplevel
        lines = self.lines()
        record = json.loads(lines[2])
        record["outcome"] = "allowed"
        record["hash"] = record_hash(record["prev"], record)
        lines[2] = json.dumps(record)
        self.write(lines)
        self.assertEqual(self.kinds(), ["chain-break"])

    def test_a_deleted_record_is_a_gap(self) -> None:
        lines = self.lines()
        del lines[2]
        self.write(lines)
        self.assertEqual(self.kinds(), ["gap"])

    def test_swapped_records_break_the_chain(self) -> None:
        lines = self.lines()
        lines[2], lines[3] = lines[3], lines[2]
        self.write(lines)
        self.assertEqual(self.kinds(), ["chain-break", "chain-break", "chain-break"])

    def test_a_torn_tail_is_reported_but_not_a_break(self) -> None:
        with self.path.open("a") as f:
            f.write('{"id":"half')
        report = verify(self.dir)
        self.assertEqual([p.kind for p in report.problems], ["truncated-tail"])
        self.assertTrue(report.ok)
        self.assertEqual(report.records, 5)

    def test_a_garbage_line_is_unreadable(self) -> None:
        lines = self.lines()
        lines.insert(2, "not json")
        self.write(lines)
        self.assertEqual(self.kinds(), ["unreadable"])

    def test_the_chain_continues_across_segments(self) -> None:
        lines = self.lines()
        self.write(lines[:3])
        self.write(lines[3:], self.dir / "29990101T000000Z.jsonl")
        report = verify(self.dir)
        self.assertTrue(report.ok)
        self.assertEqual((report.segments, report.records), (2, 5))

    def test_a_missing_segment_breaks_the_seam(self) -> None:
        lines = self.lines()
        self.write(lines[:2])
        self.write(lines[4:], self.dir / "29990101T000000Z.jsonl")
        self.assertEqual(self.kinds(), ["gap"])

    def test_a_pruned_first_segment_starts_mid_chain(self) -> None:
        lines = self.lines()
        self.write(lines[2:])
        report = verify(self.dir)
        self.assertTrue(report.ok)
        self.assertEqual(report.starts_after, json.loads(lines[1])["hash"])


if __name__ == "__main__":
    unittest.main()
