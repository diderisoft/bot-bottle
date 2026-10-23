"""Unit: importing journal segments from other hosts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bot_bottle.contract.audit import AuditEvent, canonical, record_hash
from bot_bottle.control.hostctl.audit_import import AuditImportError, import_segment
from bot_bottle.control.hostctl.audit_index import AuditIndex, IndexBreak, Query
from bot_bottle.control.hostctl.audit_journal import AuditJournal
from bot_bottle.control.hostctl.audit_verify import verify


def _event() -> AuditEvent:
    return AuditEvent(type="egress.decision", actor="a", action="egress.connect",
                      outcome="blocked", sensitivity="security", ts_event="t")


class Import(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.local = root / "local"
        AuditJournal(self.local, host="here", engine="e")
        self.index = AuditIndex(self.local)
        self.index.sync()
        self.remote = AuditJournal(root / "remote", host="there", engine="e")
        self.remote.append(_event(), producer="egress")
        self.out = root / "out"
        self.out.mkdir()

    def export(self, lines: slice) -> Path:
        """A segment file holding `lines` of the remote journal."""
        path = self.out / f"{len(list(self.out.iterdir()))}.jsonl"
        kept = self.remote.path.read_text().splitlines()[lines]
        path.write_text("".join(line + "\n" for line in kept))
        return path

    def next_segment(self, segment: str) -> Path:
        """A sealed one-record segment that continues the remote chain, as the
        writer would write it after rotating."""
        last = json.loads(self.remote.path.read_text().splitlines()[-1])
        record = {**last, "id": "33333333-3333-4333-8333-333333333333",
                  "seq": last["seq"] + 1, "segment": segment, "prev": last["hash"]}
        record["hash"] = record_hash(record["prev"], record)
        path = self.out / f"{segment}.jsonl"
        path.write_text(canonical(record) + "\n")
        return path

    def test_imports_a_foreign_chain_verbatim(self) -> None:
        source = self.export(slice(None))
        result = import_segment(self.local, source, self.index)
        self.assertEqual(result.records, 2)
        self.assertFalse(result.extended)
        self.assertEqual(result.chain.parent.name, "there")
        [stored] = list(result.chain.iterdir())  # pylint: disable=unbalanced-tuple-unpacking
        self.assertEqual(stored.read_bytes(), source.read_bytes())
        self.assertTrue(verify(result.chain).ok)
        self.assertEqual(len(self.index.query(Query(host="there"))), 2)
        self.assertEqual(len(self.index.query(Query(host="here"))), 1)

    def test_the_local_chain_is_untouched(self) -> None:
        before = [p.read_bytes() for p in sorted(self.local.glob("*.jsonl"))]
        import_segment(self.local, self.export(slice(None)), self.index)
        self.assertEqual([p.read_bytes() for p in sorted(self.local.glob("*.jsonl"))], before)

    def test_importing_the_same_segment_twice_is_a_no_op(self) -> None:
        source = self.export(slice(None))
        import_segment(self.local, source, self.index)
        again = import_segment(self.local, source, self.index)
        self.assertTrue(again.already_imported)
        self.assertEqual(len(self.index.query(Query(host="there"))), 2)

    def test_a_continuation_extends_its_chain(self) -> None:
        first = import_segment(self.local, self.export(slice(None)), self.index)
        second = import_segment(self.local, self.next_segment("29990101T000000Z"),
                                self.index)
        self.assertTrue(second.extended)
        self.assertEqual(second.chain, first.chain)
        self.assertTrue(verify(second.chain).ok)
        self.assertEqual(len(self.index.query(Query(host="there"))), 3)

    def test_a_broken_segment_is_refused(self) -> None:
        source = self.export(slice(None))
        source.write_text(source.read_text().replace("blocked", "allowed"))
        with self.assertRaises(AuditImportError):
            import_segment(self.local, source, self.index)

    def test_a_torn_segment_is_refused(self) -> None:
        source = self.export(slice(None))
        with source.open("a") as f:
            f.write('{"id":"half')
        with self.assertRaises(AuditImportError):
            import_segment(self.local, source, self.index)

    def test_an_unsafe_host_name_is_refused(self) -> None:
        source = self.export(slice(None))
        source.write_text(source.read_text().replace('"host":"there"', '"host":"../x"'))
        with self.assertRaises(AuditImportError):  # fails verification first anyway
            import_segment(self.local, source, self.index)

    def test_a_clashing_record_is_refused_and_nothing_is_written(self) -> None:
        """Same host, epoch and seq as a record already indexed, different id."""
        other = AuditJournal(self.out / "dup", host="here", engine="e")
        with self.assertRaises(IndexBreak):
            import_segment(self.local, other.path, self.index)
        self.assertFalse((self.local / "imported").exists())

    def test_rebuild_replays_imported_chains(self) -> None:
        import_segment(self.local, self.export(slice(None)), self.index)
        self.assertEqual(self.index.rebuild(), 3)


if __name__ == "__main__":
    unittest.main()
