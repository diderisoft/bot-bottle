"""Offline check of the audit journal's hash chain.

Needs only the segment files: every record's `hash` is recomputed and
checked against the next record's `prev`, across segment seams, so an edit,
deletion or reorder shows up at the record where the chain breaks.

What it reports, and what each one means:

- `truncated-tail`: the last segment ends in a partial line. A crash
  mid-append, not tampering; the writer cuts it on its next start.
- `unreadable`: a complete line that isn't a JSON record.
- `edited`: a record no longer hashes to its own `hash`.
- `gap`: records are missing from inside an epoch (`seq` skipped forward,
  and never goes back).
- `chain-break`: `prev` doesn't match the record before it, for any other
  reason (a reorder, an insert, records missing across an epoch boundary).

After a break, checking continues from the broken record's own `hash`, so one
pass finds every break rather than only the first.

A chain doesn't record its own length, so a cut that removes whole records
from the end of the newest segment leaves a valid, shorter chain. Catching
that needs the head anchored somewhere else (an exporter's cursor, or the
external anchoring planned as a follow-up).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...contract.audit import record_hash
from .audit_journal import SEGMENT_SUFFIX


@dataclass(frozen=True)
class Problem:
    kind: str
    segment: str
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.segment}:{self.line}: {self.detail}"


@dataclass
class VerifyReport:
    segments: int = 0
    records: int = 0
    head: str = ""
    # `prev` of the first record. Empty for a chain that starts at genesis;
    # set when older segments were pruned and the journal starts mid-chain.
    starts_after: str = ""
    problems: list[Problem] = field(default_factory=list)

    @property
    def breaks(self) -> list[Problem]:
        """Problems that mean the record trail was altered, not just a crash."""
        return [p for p in self.problems if p.kind != "truncated-tail"]

    @property
    def ok(self) -> bool:
        return not self.breaks


def _check(record: dict[str, Any], before: dict[str, Any] | None,
           prev: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if record_hash(str(record.get("prev", "")), record) != record.get("hash"):
        found.append(("edited", f"record {record.get('id')} does not match its hash"))
    if before is not None and record.get("prev") != prev:
        same_epoch = record.get("epoch") == before.get("epoch")
        skipped = same_epoch and int(record.get("seq", 0)) > int(before.get("seq", 0)) + 1
        if skipped:
            found.append(("gap", f"seq {before.get('seq')} -> {record.get('seq')} "
                                 f"in epoch {record.get('epoch')}"))
        else:
            found.append(("chain-break", f"record {record.get('id')} does not "
                                         f"chain onto the record before it"))
    return found


def verify(directory: Path) -> VerifyReport:
    """Check every segment in `directory` as one chain."""
    return verify_segments(sorted(directory.glob(f"*{SEGMENT_SUFFIX}")))


def verify_segments(segments: list[Path]) -> VerifyReport:
    """Check `segments`, in the order given, as one chain."""
    report = VerifyReport()
    gap_epochs: list[tuple[int, Any]] = []  # (index into problems, epoch)
    reordered: set[Any] = set()
    report.segments = len(segments)
    prev = ""
    before: dict[str, Any] | None = None
    for index, path in enumerate(segments):
        name = path.name
        data = path.read_bytes()
        lines = data.split(b"\n")
        torn = lines.pop()  # b"" when the file ends in a newline
        if torn:
            last_segment = index == len(segments) - 1
            kind = "truncated-tail" if last_segment else "unreadable"
            report.problems.append(Problem(
                kind, name, len(lines) + 1, f"partial final line ({len(torn)} bytes)"))
        for number, raw in enumerate(lines, start=1):
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("not an object")
            except ValueError:
                report.problems.append(Problem("unreadable", name, number,
                                               "not a JSON record"))
                continue
            if before is None:
                report.starts_after = str(record.get("prev", ""))
            for kind, detail in _check(record, before, prev):
                if kind == "gap":
                    gap_epochs.append((len(report.problems), record.get("epoch")))
                report.problems.append(Problem(kind, name, number, detail))
            if (before is not None and record.get("epoch") == before.get("epoch")
                    and int(record.get("seq", 0)) <= int(before.get("seq", 0))):
                reordered.add(record.get("epoch"))
            prev = str(record.get("hash", ""))
            before = record
            report.records += 1
    # A seq that jumps forward and later comes back is a reorder, not a gap.
    for i, epoch in gap_epochs:
        if epoch in reordered:
            p = report.problems[i]
            report.problems[i] = Problem("chain-break", p.segment, p.line, p.detail)
    report.head = prev
    return report


__all__ = ["Problem", "VerifyReport", "verify", "verify_segments"]
