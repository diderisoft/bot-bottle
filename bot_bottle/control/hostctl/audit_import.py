"""Import a journal segment from another host or a backup.

Segments are stored verbatim under `imported/<host>/<chain>/`, never grafted
onto the local chain, so they stay verifiable on their own. One whose first
`prev` is the head of an imported chain from that host extends it; otherwise it
starts a chain named after its first record's hash.
"""

from __future__ import annotations

import filecmp
import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit_index import IMPORTED_DIRNAME, AuditIndex, imported_chains
from .audit_journal import SEGMENT_SUFFIX, complete_lines
from .audit_verify import verify, verify_segments


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AuditImportError(Exception):
    """The segment can't be imported as-is."""


@dataclass(frozen=True)
class ImportResult:
    chain: Path
    records: int
    extended: bool  # continued an imported chain
    already_imported: bool  # imported before


def _records(source: Path) -> Iterator[tuple[dict[str, Any], str]]:
    """Each record with its line as stored, one at a time: a segment can be
    gigabytes."""
    for raw in complete_lines(source):
        line = raw.decode("utf-8").rstrip("\n")
        yield json.loads(line), line


@dataclass(frozen=True)
class _Segment:
    first: dict[str, Any]
    host: str
    segment: str
    records: int


def _read(source: Path) -> _Segment:
    report = verify_segments([source])
    if report.problems:
        raise AuditImportError(
            f"{source.name} doesn't verify: " + "; ".join(str(p) for p in report.problems))
    first: dict[str, Any] | None = None
    hosts: set[str] = set()
    names: set[str] = set()
    count = 0
    for record, _ in _records(source):
        first = first or record
        hosts.add(str(record.get("host", "")))
        names.add(str(record.get("segment", "")))
        count += 1
    if first is None:
        raise AuditImportError(f"{source.name} is empty")
    return _Segment(first, _single(hosts, "host"), _single(names, "segment"), count)


def _single(values: set[str], field: str) -> str:
    if len(values) != 1:
        raise AuditImportError(f"a segment must have one {field}, found {sorted(values)}")
    value = values.pop()
    if not _NAME.match(value):
        raise AuditImportError(f"{field} {value!r} isn't a safe name")
    return value


def import_segment(journal_dir: Path, source: Path, index: AuditIndex) -> ImportResult:
    read = _read(source)
    host, segment, first = read.host, read.segment, read.first
    host_dir = journal_dir / IMPORTED_DIRNAME / host

    chain: Path | None = None
    extended = False
    first_prev = str(first.get("prev", ""))
    if first_prev:
        for candidate in imported_chains(journal_dir):
            if candidate.parent == host_dir and verify(candidate).head == first_prev:
                chain, extended = candidate, True
                break
    if chain is None:
        chain = host_dir / str(first["hash"])[:16]

    dest = chain / f"{segment}{SEGMENT_SUFFIX}"
    if dest.exists():
        if filecmp.cmp(dest, source, shallow=False):
            return ImportResult(chain, read.records, extended, already_imported=True)
        raise AuditImportError(f"{dest} already holds a different segment")
    existing = sorted(p.name for p in chain.glob(f"*{SEGMENT_SUFFIX}")) if chain.is_dir() else []
    if existing and dest.name <= existing[-1]:
        raise AuditImportError(
            f"segment {segment} would sort before {existing[-1]} in its chain")

    index.add_records(_records(source))  # all or none; refuses a (host, epoch, seq) clash
    for directory in (journal_dir / IMPORTED_DIRNAME, host_dir, chain):
        directory.mkdir(mode=0o700, exist_ok=True)
    shutil.copyfile(source, dest)
    dest.chmod(0o600)
    return ImportResult(chain, read.records, extended, already_imported=False)


__all__ = ["AuditImportError", "ImportResult", "import_segment"]
