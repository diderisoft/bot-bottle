"""The host's single audit writer: an append-only JSONL journal in segment
files under `host_audit_dir()`, each record chained to the previous across
segments and restarts.

On start the writer adopts the last intact record's `hash` as `prev` and `epoch
+ 1`, resets `seq`, and cuts a torn final line. `id` is the idempotency key;
known ids are read from the journal on start. Redaction happens here:
credential-shaped keys are refused and token-shaped values in `resource`,
`payload` and `untrusted` scrubbed. An exclusive directory lock refuses a
second writer.

A new segment starts once the current one reaches `SEGMENT_MAX_BYTES` or a
record arrives on a later UTC day than the segment's name. Segment names are
UTC start times, so they sort in chain order. `prune` drops whole old
segments, never the current one, after appending a `journal.pruned` record
that names the dropped range, its last hash and each dropped segment's last
hash: verification of what's left starts from that record, even if a crash
stopped the deletes partway.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...contract.audit import SCHEMA_VERSION, AuditEvent, canonical, record_hash, utc_iso
from ...gateway.egress.dlp_detectors import redact_tokens
from ...lib.log import warn


SEGMENT_SUFFIX = ".jsonl"
# A segment this large is closed and the next record starts a new one.
SEGMENT_MAX_BYTES = 64 * 1024 * 1024
# `payload.cause` of the record the writer appends after cutting a torn tail.
TORN_TAIL = "torn_tail"
# The record `prune` appends before deleting segments.
PRUNED = "journal.pruned"
_LOCK_NAME = ".writer.lock"
_SEGMENT_FORMAT = "%Y%m%dT%H%M%SZ"

# Key names (and suffixes) that only ever hold a credential.
_DENIED_KEYS = frozenset({
    "token", "secret", "password", "passwd", "authorization", "cookie",
    "api_key", "private_key",
})
_DENIED_SUFFIXES = ("_key", "_token", "_secret", "_password")


class AuditJournalBusy(RuntimeError):
    """Another writer holds the journal; a second would fork the chain."""


class AuditRedactionError(ValueError):
    """A producer put a credential-shaped key in an event: pass a reference,
    never the value."""


def _check_keys(value: Any, where: str) -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            name = str(key).lower()
            if name in _DENIED_KEYS or name.endswith(_DENIED_SUFFIXES):
                raise AuditRedactionError(f"credential-shaped key {where}.{key}")
            _check_keys(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            _check_keys(inner, f"{where}[{i}]")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact_tokens(value)
    if isinstance(value, Mapping):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


# Producer fields that can carry free text.
_SCRUBBED_FIELDS = ("resource", "payload", "untrusted")


def redact(event: AuditEvent) -> AuditEvent:
    """The event as it may be written: refused on a credential-shaped key, with
    token-shaped values in `resource`, `payload` and `untrusted` scrubbed (not
    dropped)."""
    _check_keys(event.payload, "payload")
    _check_keys(event.untrusted, "untrusted")
    body = event.to_json()
    scrubbed = {name: _scrub(body[name]) for name in _SCRUBBED_FIELDS}
    if all(scrubbed[name] == body[name] for name in _SCRUBBED_FIELDS):
        return event
    return AuditEvent.from_json({**body, **scrubbed})


def _segment_id(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(_SEGMENT_FORMAT)


def segment_start(name: str) -> float | None:
    """When the segment `name` (with or without its suffix) was opened, from
    its name; None for a name the writer didn't give."""
    try:
        return datetime.strptime(name.removesuffix(SEGMENT_SUFFIX), _SEGMENT_FORMAT) \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _next_segment_id(now: float, current: str) -> str:
    """A name for a segment opened at `now` that sorts after `current`, even
    within the same second or after the clock stepped back."""
    name = _segment_id(now)
    started = segment_start(current) if current else None
    if started is not None and name <= current:
        name = _segment_id(started + 1)
    return name


def segments(directory: Path) -> list[Path]:
    """The journal's segment files, oldest first."""
    return sorted(directory.glob(f"*{SEGMENT_SUFFIX}"))


def _cut_torn_tail(path: Path) -> int:
    """Cut a partial final line left by a crash. Returns bytes cut."""
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return 0
    keep = data.rfind(b"\n") + 1
    warn(f"audit journal {path.name}: dropping a torn final line "
         f"({len(data) - keep} bytes) left by an interrupted append")
    with path.open("r+b") as f:
        f.truncate(keep)
    return len(data) - keep


def _hold_writer_lock(directory: Path) -> int:
    """An fd holding an exclusive lock on the journal dir, released at exit."""
    fd = os.open(directory / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AuditJournalBusy(
            f"another audit writer holds {directory}; there is one per host"
        ) from None
    return fd


def _ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        for raw in path.read_bytes().splitlines():
            ids.add(str(json.loads(raw).get("id")))
    return ids


def first_record(path: Path) -> dict[str, Any] | None:
    """A segment's first complete record, or None when it has none."""
    with path.open("rb") as f:
        line = f.readline()
    return json.loads(line) if line.endswith(b"\n") else None


def last_record(path: Path, chunk: int = 1 << 16) -> dict[str, Any] | None:
    """A segment's last complete record, read back from the end (a segment
    can be large), or None when it has none."""
    with path.open("rb") as f:
        pos = f.seek(0, os.SEEK_END)
        data = b""
        # Two newlines hold the last complete line whole.
        while pos > 0 and data.count(b"\n") < 2:
            start = max(0, pos - chunk)
            f.seek(start)
            data = f.read(pos - start) + data
            pos = start
    lines = data[:data.rfind(b"\n") + 1].splitlines()
    return json.loads(lines[-1]) if lines else None


class AuditJournal:
    """Appends sealed records to the host's journal. One per host."""

    def __init__(
        self,
        directory: Path,
        *,
        host: str,
        engine: str,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        max_segment_bytes: int = SEGMENT_MAX_BYTES,
    ) -> None:
        self._dir = directory
        self._host = host
        self._engine = engine
        self._clock = clock
        self._monotonic = monotonic
        self._max_segment_bytes = max_segment_bytes
        self._boot = monotonic()
        self._lock = threading.Lock()
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._writer_lock = _hold_writer_lock(self._dir)

        existing = segments(self._dir)
        last: dict[str, Any] | None = None
        cut, torn_segment = 0, ""
        if existing:
            cut = _cut_torn_tail(existing[-1])
            torn_segment = existing[-1].name.removesuffix(SEGMENT_SUFFIX)
            for path in reversed(existing):
                last = last_record(path)
                if last is not None:
                    break
        self._ids = _ids(existing)
        self._seq = 0
        self._segment = ""
        if last is None:
            self._epoch = 0
            self._prev = ""
            self._open_segment()
        else:
            self._epoch = int(last["epoch"]) + 1
            self._prev = str(last["hash"])
            self._path = existing[-1]
            self._segment = self._path.name.removesuffix(SEGMENT_SUFFIX)
            self._size = self._path.stat().st_size
        if cut:
            # Record that a crash cut the tail, since verify can't see it
            # afterwards.
            self._write(AuditEvent(
                type="audit.truncation_detected", actor="host",
                action="audit.truncation_detected", outcome="failure", sensitivity="security",
                ts_event=utc_iso(self._clock()), resource=torn_segment,
                payload={"cause": TORN_TAIL, "segment": torn_segment, "dropped_bytes": cut,
                         "last_epoch": last["epoch"] if last else None,
                         "last_seq": last["seq"] if last else None},
            ), producer="audit")

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        """Release the writer lock. The journal can't be appended to after."""
        os.close(self._writer_lock)

    def _open_segment(self) -> None:
        now = self._clock()
        previous = self._segment
        self._segment = _next_segment_id(now, previous)
        self._path = self._dir / f"{self._segment}{SEGMENT_SUFFIX}"
        self._size = 0
        self._write(AuditEvent(
            type="audit.segment_open", actor="host", action="audit.segment_open",
            outcome="success", sensitivity="security", ts_event=utc_iso(now),
            payload={"segment": self._segment, **({"previous": previous} if previous else {})},
        ), producer="audit", rotate=False)

    def _segment_due(self) -> bool:
        """Whether the next record starts a new segment: this one is full, or
        it's a later UTC day than the segment's name."""
        return (self._size >= self._max_segment_bytes
                or _segment_id(self._clock())[:8] > self._segment[:8])

    def prune(self, *, max_age_days: int = 0, max_bytes: int = 0,
              keep_from: str = "") -> dict[str, Any] | None:
        """Drop the oldest whole segments that started more than
        `max_age_days` ago or put the journal over `max_bytes` (0: no limit).
        Never the current segment, nor `keep_from` (a segment's file name) or
        any after it: the oldest a reader hasn't finished. "" keeps all.

        Appends `journal.pruned` first, naming the segments, the first and
        last dropped records and the last one's hash, which the oldest kept
        record's `prev` carries, and each dropped segment's last hash, which
        it carries if a crash stopped the deletes partway. Returns that record, or None when nothing
        went."""
        with self._lock:
            now = self._clock()
            existing = segments(self._dir)
            sizes = {path: path.stat().st_size for path in existing}
            total = sum(sizes.values())
            drop: list[Path] = []
            # A segment's records are older than the next one's start.
            for path, after in zip(existing, existing[1:]):
                if path == self._path or path.name >= keep_from:
                    break
                ended = segment_start(after.name)
                too_old = bool(max_age_days) and ended is not None \
                    and ended <= now - max_age_days * 86400
                too_big = bool(max_bytes) and total > max_bytes
                if not (too_old or too_big):
                    break
                drop.append(path)
                total -= sizes[path]
            first = next((r for r in map(first_record, drop) if r is not None), None)
            # Each segment's last record: segments are deleted one at a time,
            # oldest first, so a crash partway leaves a chain starting after
            # any of them.
            lasts = [r for r in map(last_record, drop) if r is not None]
            if first is None or not lasts:
                return None
            last = lasts[-1]
            record = self._write(AuditEvent(
                type=PRUNED, actor="host", action=PRUNED, outcome="success",
                sensitivity="security", ts_event=utc_iso(now),
                payload={"segments": [p.name.removesuffix(SEGMENT_SUFFIX) for p in drop],
                         "bytes": sum(sizes[p] for p in drop),
                         "first": _position(first), "last": _position(last),
                         "last_hash": str(last["hash"]),
                         "segment_last_hashes": [str(r["hash"]) for r in lasts],
                         "max_age_days": max_age_days, "max_bytes": max_bytes},
            ), producer="audit")
            for path in drop:
                path.unlink(missing_ok=True)
            return record

    def append(self, event: AuditEvent, *, producer: str) -> dict[str, Any] | None:
        """Seal `event` and write it. Returns the record, or None for a retry
        of an id already written."""
        event = redact(event)
        with self._lock:
            if event.id in self._ids:
                return None
            return self._write(event, producer=producer)

    def _write(self, event: AuditEvent, *, producer: str,
               rotate: bool = True) -> dict[str, Any]:
        if rotate and self._segment_due():
            self._open_segment()
        record: dict[str, Any] = {
            **event.to_json(),
            "v": SCHEMA_VERSION,
            "epoch": self._epoch,
            "seq": self._seq,
            "segment": self._segment,
            "prev": self._prev,
            "hash": "",
            "ts_recorded": utc_iso(self._clock()),
            "ts_mono": round(self._monotonic() - self._boot, 3),
            "producer": producer,
            "host": self._host,
            "engine": self._engine,
        }
        record["hash"] = record_hash(self._prev, record)
        line = (canonical(record) + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = 0
            while written < len(line):  # writes may be partial
                written += os.write(fd, line[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        self._prev = record["hash"]
        self._seq += 1
        self._size += len(line)
        self._ids.add(event.id)
        return record


def _position(record: dict[str, Any]) -> dict[str, Any]:
    return {"epoch": record.get("epoch"), "seq": record.get("seq"),
            "ts": record.get("ts_recorded")}


__all__ = [
    "AuditJournal",
    "AuditJournalBusy",
    "AuditRedactionError",
    "PRUNED",
    "SEGMENT_MAX_BYTES",
    "SEGMENT_SUFFIX",
    "TORN_TAIL",
    "first_record",
    "last_record",
    "redact",
    "segment_start",
    "segments",
]
