"""Unit: the host's single audit writer (PRD 0089)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bot_bottle.contract.audit import AuditEvent, canonical, record_hash
from bot_bottle.control.hostctl.audit_journal import (
    AuditJournal,
    AuditRedactionError,
    redact,
)


# Built at runtime so the source never holds a token-shaped literal.
_FAKE_TOKEN = "ghp_" + "a" * 36


def _event(**overrides: Any) -> AuditEvent:
    base: dict[str, Any] = dict(
        type="egress.decision", actor="bottled-agent:amber-fox-12",
        action="egress.connect", outcome="blocked", sensitivity="security",
        ts_event="2026-07-26T18:22:04.061Z", bottle="amber-fox")
    base.update(overrides)
    return AuditEvent(**base)


def _records(directory: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines())
    return out


def _assert_chained(case: unittest.TestCase, records: list[dict[str, Any]]) -> None:
    prev = ""
    for r in records:
        case.assertEqual(r["prev"], prev)
        case.assertEqual(r["hash"], record_hash(prev, r))
        prev = r["hash"]


class _JournalCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "audit"
        self.now = 1_785_000_000.0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def journal(self) -> AuditJournal:
        return AuditJournal(self.dir, host="mac-studio-1", engine="bot-bottle/0.1.4",
                            clock=lambda: self.now)


class FreshJournal(_JournalCase):
    def test_opens_a_segment_with_a_genesis_record(self) -> None:
        self.journal()
        [genesis] = _records(self.dir)
        self.assertEqual(genesis["type"], "audit.segment_open")
        self.assertEqual((genesis["epoch"], genesis["seq"], genesis["prev"]), (0, 0, ""))
        self.assertEqual(genesis["payload"], {"segment": genesis["segment"]})

    def test_appends_chain_in_order(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        j.append(_event(outcome="allowed"), producer="egress")
        records = _records(self.dir)
        self.assertEqual([r["seq"] for r in records], [0, 1, 2])
        _assert_chained(self, records)

    def test_the_writer_sets_attribution_not_the_producer(self) -> None:
        record = self.journal().append(_event(), producer="egress")
        self.assertEqual(record["producer"], "egress")
        self.assertEqual(record["host"], "mac-studio-1")
        self.assertEqual(record["engine"], "bot-bottle/0.1.4")
        self.assertEqual(record["v"], 1)

    def test_each_line_is_the_canonical_record(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        lines = j.path.read_text().splitlines()
        self.assertEqual(lines[-1], canonical(json.loads(lines[-1])))

    def test_the_journal_is_private(self) -> None:
        j = self.journal()
        self.assertEqual(j.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)


class Restart(_JournalCase):
    def test_continues_the_chain_in_a_new_epoch(self) -> None:
        first = self.journal()
        first.append(_event(), producer="egress")
        second = self.journal()
        record = second.append(_event(), producer="egress")
        self.assertEqual((record["epoch"], record["seq"]), (1, 0))
        self.assertEqual(second.path, first.path)  # no new segment on restart
        _assert_chained(self, _records(self.dir))

    def test_epoch_comes_from_the_last_written_record(self) -> None:
        for _ in range(3):
            self.journal().append(_event(), producer="egress")
        record = self.journal().append(_event(), producer="egress")
        self.assertEqual(record["epoch"], 3)

    def test_a_boot_that_wrote_nothing_does_not_use_up_an_epoch(self) -> None:
        """Nothing was written in it, so reusing its number can't collide."""
        self.journal()
        self.journal()
        record = self.journal().append(_event(), producer="egress")
        self.assertEqual(record["epoch"], 1)

    def test_a_torn_final_line_is_cut_before_appending(self) -> None:
        j = self.journal()
        j.append(_event(), producer="egress")
        with j.path.open("a") as f:
            f.write('{"id":"half-writ')  # a crash mid-append
        record = self.journal().append(_event(), producer="egress")
        records = _records(self.dir)
        self.assertEqual(records[-1], record)
        _assert_chained(self, records)


class Redaction(unittest.TestCase):
    def test_a_credential_shaped_key_is_refused(self) -> None:
        for key in ("token", "Authorization", "api_key", "github_token", "db_password"):
            with self.subTest(key=key), self.assertRaises(AuditRedactionError):
                redact(_event(payload={key: "x"}))

    def test_a_nested_credential_key_is_refused(self) -> None:
        with self.assertRaises(AuditRedactionError):
            redact(_event(untrusted={"headers": [{"cookie": "x"}]}))

    def test_a_reference_to_a_credential_is_allowed(self) -> None:
        event = _event(payload={"token_id": "t-1", "key_fingerprint": "sha256:ab"})
        self.assertEqual(redact(event), event)

    def test_a_token_shaped_value_in_untrusted_is_scrubbed(self) -> None:
        event = redact(_event(untrusted={"reason": f"use {_FAKE_TOKEN} please"}))
        assert event.untrusted is not None
        self.assertNotIn(_FAKE_TOKEN, event.untrusted["reason"])
        self.assertIn("********", event.untrusted["reason"])

    def test_scrubbed_events_are_kept_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            j = AuditJournal(Path(tmp), host="h", engine="e")
            j.append(_event(untrusted={"reason": _FAKE_TOKEN}), producer="egress")
            self.assertNotIn(_FAKE_TOKEN, j.path.read_text())
            self.assertEqual(len(j.path.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
