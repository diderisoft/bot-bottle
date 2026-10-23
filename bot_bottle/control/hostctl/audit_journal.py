"""The host's single audit writer.

One append-only JSONL journal per host, split into segment files under
`host_audit_dir()`. Every record chains onto the one before it, across
segments and restarts, so an edit, deletion or reorder breaks the chain.

The journal is the only place the chain head and `epoch` live. On start the
writer reads the last intact record, adopts its `hash` as the next `prev` and
its `epoch + 1` as the new epoch, and resets `seq`. A torn final line (a crash
mid-append) is cut back to the last intact record before appending, since a
line glued onto it would corrupt the next record too.

`id` is the idempotency key: a producer retrying an event reuses it, and the
writer drops an id it has already written. The ids are read from the journal
on start, which holds every record while no segment is ever pruned.

Redaction runs here, before anything is written: a credential-shaped key is
refused, and token-shaped values in the producer-supplied fields (`resource`,
`payload`, `untrusted`) are scrubbed.

"One writer per host" is enforced, not assumed: the writer holds an exclusive
lock on the journal directory for its lifetime, so a second writer (a second
host controller, a stray CLI) is refused instead of forking the chain.
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

from ...contract.audit import SCHEMA_VERSION, AuditEvent, canonical, record_hash
from ...gateway.egress.dlp_detectors import redact_tokens
from ...lib.log import warn


SEGMENT_SUFFIX = ".jsonl"
_LOCK_NAME = ".writer.lock"

# Exact key names that only ever hold a credential, plus suffixes that do.
_DENIED_KEYS = frozenset({
    "token", "secret", "password", "passwd", "authorization", "cookie",
    "api_key", "private_key",
})
_DENIED_SUFFIXES = ("_key", "_token", "_secret", "_password")


class AuditJournalBusy(RuntimeError):
    """Another writer holds the journal. Two writers would both chain onto
    the same head and fork the chain, so the second one is refused."""


class AuditRedactionError(ValueError):
    """A producer put a credential-shaped key in an event. Always a producer
    bug: pass a reference (a token id or fingerprint), never the value."""


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


# Producer-supplied fields that can carry free text: an egress URL, a reason,
# a commit message. The rest are enums, ids and digests.
_SCRUBBED_FIELDS = ("resource", "payload", "untrusted")


def redact(event: AuditEvent) -> AuditEvent:
    """The event as it may be written: refused on a credential-shaped key,
    with token-shaped values in `resource`, `payload` and `untrusted`
    scrubbed (kept, not dropped, so a producer bug can't make an event
    vanish)."""
    _check_keys(event.payload, "payload")
    _check_keys(event.untrusted, "untrusted")
    body = event.to_json()
    scrubbed = {name: _scrub(body[name]) for name in _SCRUBBED_FIELDS}
    if all(scrubbed[name] == body[name] for name in _SCRUBBED_FIELDS):
        return event
    return AuditEvent.from_json({**body, **scrubbed})


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def _segment_id(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _segments(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"*{SEGMENT_SUFFIX}"))


def _cut_torn_tail(path: Path) -> None:
    """Drop a partial final line left by a crash mid-append."""
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    keep = data.rfind(b"\n") + 1
    warn(f"audit journal {path.name}: dropping a torn final line "
         f"({len(data) - keep} bytes) left by an interrupted append")
    with path.open("r+b") as f:
        f.truncate(keep)


def _hold_writer_lock(directory: Path) -> int:
    """An fd holding an exclusive lock on the journal directory. The kernel
    releases it when the process exits, however it exits."""
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
        if segments:
            _cut_torn_tail(segments[-1])
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
            outcome="success", sensitivity="security", ts_event=_utc_iso(now),
            payload={"segment": self._segment},
        ), producer="audit")

    def append(self, event: AuditEvent, *, producer: str) -> dict[str, Any] | None:
        """Seal `event` onto the chain and write it. Returns the record, or
        None when an event with its id was already written (a retry)."""
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
            "ts_recorded": _utc_iso(self._clock()),
            "ts_mono": round(self._monotonic() - self._boot, 3),
            "producer": producer,
            "host": self._host,
            "engine": self._engine,
        }
        record["hash"] = record_hash(self._prev, record)
        line = (canonical(record) + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
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
    "redact",
]
