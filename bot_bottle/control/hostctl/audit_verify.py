"""Offline check of the audit journal's hash chain, from the segment files
alone. Reports:

- `truncated-tail`: a partial last line, or a recorded `torn_tail` cut. A
  crash, not tampering.
- `unreadable`: a complete line that isn't a JSON record.
- `edited`: a record no longer hashes to its `hash`.
- `gap`: `seq` skips forward within an epoch.
- `chain-break`: `prev` doesn't match for any other reason (reorder, insert,
  loss across epochs).
- `unrecorded-prune` (`anchored` only): the chain starts after a record no
  `journal.pruned` record names, so older segments went without the writer
  pruning them.

Checking continues past a break, so one pass finds them all. A chain whose
older segments were pruned starts from the `journal.pruned` record naming the
hash its first record's `prev` carries: `last_hash`, or one of
`segment_last_hashes` when a crash stopped the deletes partway. Whole records cut from the end
of the newest segment leave a valid shorter chain; catching that needs the
head anchored elsewhere (an exporter's cursor).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...contract.audit import record_hash
from .audit_journal import PRUNED, TORN_TAIL, segments as journal_segments


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
    # `prev` of the first record: empty at genesis, set when older segments
    # were pruned.
    starts_after: str = ""
    # Where the `journal.pruned` record naming `starts_after` is, as
    # `segment:line`; "" when there's none.
    pruned_at: str = ""
    problems: list[Problem] = field(default_factory=list)

    @property
    def breaks(self) -> list[Problem]:
        """Problems meaning the trail was altered, not just a crash."""
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


def _names(pruned: dict[str, Any], start: str) -> bool:
    """Whether a `journal.pruned` payload names the hash `start` a chain
    starts after: its last one, or any dropped segment's, which is where the
    chain starts if a crash stopped the deletes partway."""
    hashes = pruned.get("segment_last_hashes")
    return pruned.get("last_hash") == start or (
        isinstance(hashes, list) and start in hashes)


def verify(directory: Path, *, anchored: bool = False) -> VerifyReport:
    """Check every segment in `directory` as one chain. `anchored`: the chain
    is a whole journal (the host's own), so a start after anything but a
    recorded prune is a break. An imported chain may start anywhere."""
    return verify_segments(journal_segments(directory), anchored=anchored)


def _lines(path: Path) -> Iterator[bytes]:
    """A segment's lines, newline kept, one at a time: a segment can be
    gigabytes. Only a torn final line lacks the newline."""
    with path.open("rb") as f:
        yield from f


def verify_segments(segments: list[Path], *, anchored: bool = False) -> VerifyReport:
    """Check `segments`, in the order given, as one chain."""
    report = VerifyReport()
    gap_epochs: list[tuple[int, Any]] = []  # (index into problems, epoch)
    reordered: set[Any] = set()
    report.segments = len(segments)
    prev = ""
    before: dict[str, Any] | None = None
    for index, path in enumerate(segments):
        name = path.name
        torn: tuple[int, int] | None = None  # (line number, bytes)
        for number, raw in enumerate(_lines(path), start=1):
            if not raw.endswith(b"\n"):
                torn = (number, len(raw))
                break
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
            payload = record.get("payload") or {}
            if record.get("type") == PRUNED and isinstance(payload, dict) and \
                    report.starts_after and _names(payload, report.starts_after):
                report.pruned_at = f"{name}:{number}"
            if record.get("type") == "audit.truncation_detected" and \
                    isinstance(payload, dict) and payload.get("cause") == TORN_TAIL:
                report.problems.append(Problem(
                    "truncated-tail", name, number,
                    f"the writer cut {payload.get('dropped_bytes')} torn bytes after a crash"))
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
        if torn:
            last_segment = index == len(segments) - 1
            kind = "truncated-tail" if last_segment else "unreadable"
            report.problems.append(Problem(
                kind, name, torn[0], f"partial final line ({torn[1]} bytes)"))
    # A seq that jumps forward and later comes back is a reorder.
    for i, epoch in gap_epochs:
        if epoch in reordered:
            p = report.problems[i]
            report.problems[i] = Problem("chain-break", p.segment, p.line, p.detail)
    if anchored and report.starts_after and not report.pruned_at and segments:
        report.problems.insert(0, Problem(
            "unrecorded-prune", segments[0].name, 1,
            f"the chain starts after {report.starts_after[:16]}, which no "
            f"{PRUNED} record names"))
    report.head = prev
    return report


__all__ = ["Problem", "VerifyReport", "verify", "verify_segments"]
