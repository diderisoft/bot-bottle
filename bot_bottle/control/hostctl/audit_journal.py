"""The host's single audit writer: an append-only JSONL journal in segment
files under `host_audit_dir()`, each record chained to the previous across
segments and restarts.

On start the writer adopts the last intact record's `hash` as `prev` and `epoch
+ 1`, resets `seq`, and cuts a torn final line. `id` is the idempotency key;
known ids are read from the journal on start. Redaction happens here:
credential-shaped keys are refused and token-shaped values in `resource`,
`payload` and `untrusted` scrubbed. An exclusive directory lock refuses a
second writer.
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
# `payload.cause` of the record the writer appends after cutting a torn tail.
TORN_TAIL = "torn_tail"
_LOCK_NAME = ".writer.lock"

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
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _segments(directory: Path) -> list[Path]:
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


def _ids(segments: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in segments:
        for raw in path.read_bytes().splitlines():
            ids.add(str(json.loads(raw).get("id")))
    return ids


def _last_record(path: Path) -> dict[str, Any] | None:
    lines = path.read_bytes().splitlines()
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
    ) -> None:
        self._dir = directory
        self._host = host
        self._engine = engine
        self._clock = clock
        self._monotonic = monotonic
        self._boot = monotonic()
        self._lock = threading.Lock()
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._writer_lock = _hold_writer_lock(self._dir)

        segments = _segments(self._dir)
        last: dict[str, Any] | None = None
        cut, torn_segment = 0, ""
        if segments:
            cut = _cut_torn_tail(segments[-1])
            torn_segment = segments[-1].name.removesuffix(SEGMENT_SUFFIX)
            for path in reversed(segments):
                last = _last_record(path)
                if last is not None:
                    break
        self._ids = _ids(segments)
        self._seq = 0
        if last is None:
            self._epoch = 0
            self._prev = ""
            self._open_segment()
        else:
            self._epoch = int(last["epoch"]) + 1
            self._prev = str(last["hash"])
            self._path = segments[-1]
            self._segment = self._path.name.removesuffix(SEGMENT_SUFFIX)
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
        self._segment = _segment_id(now)
        self._path = self._dir / f"{self._segment}{SEGMENT_SUFFIX}"
        self._write(AuditEvent(
            type="audit.segment_open", actor="host", action="audit.segment_open",
            outcome="success", sensitivity="security", ts_event=utc_iso(now),
            payload={"segment": self._segment},
        ), producer="audit")

    def append(self, event: AuditEvent, *, producer: str) -> dict[str, Any] | None:
        """Seal `event` and write it. Returns the record, or None for a retry
        of an id already written."""
        event = redact(event)
        with self._lock:
            if event.id in self._ids:
                return None
            return self._write(event, producer=producer)

    def _write(self, event: AuditEvent, *, producer: str) -> dict[str, Any]:
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
        self._ids.add(event.id)
        return record


__all__ = [
    "AuditJournal",
    "AuditJournalBusy",
    "AuditRedactionError",
    "SEGMENT_SUFFIX",
    "TORN_TAIL",
    "redact",
]
