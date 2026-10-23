"""The host's single audit writer (PRD 0089).

One append-only JSONL journal per host, split into segment files under
`host_audit_dir()`. Every record chains onto the one before it, across
segments and restarts, so an edit, deletion or reorder breaks the chain.

The journal is the only place the chain head and `epoch` live. On start the
writer reads the last intact record, adopts its `hash` as the next `prev` and
its `epoch + 1` as the new epoch, and resets `seq`. A torn final line (a crash
mid-append) is cut back to the last intact record before appending, since a
line glued onto it would corrupt the next record too.

Redaction runs here, before anything is written: a credential-shaped key is
refused, and token-shaped values in `untrusted` are scrubbed.
"""

from __future__ import annotations

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

# Exact key names that only ever hold a credential, plus suffixes that do.
_DENIED_KEYS = frozenset({
    "token", "secret", "password", "passwd", "authorization", "cookie",
    "api_key", "private_key",
})
_DENIED_SUFFIXES = ("_key", "_token", "_secret", "_password")


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


def redact(event: AuditEvent) -> AuditEvent:
    """The event as it may be written: refused on a credential-shaped key,
    with token-shaped values in `untrusted` scrubbed."""
    _check_keys(event.payload, "payload")
    _check_keys(event.untrusted, "untrusted")
    if not event.untrusted:
        return event
    body = event.to_json()
    body["untrusted"] = _scrub(event.untrusted)
    return AuditEvent.from_json(body)


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

        segments = _segments(self._dir)
        last: dict[str, Any] | None = None
        if segments:
            _cut_torn_tail(segments[-1])
            for path in reversed(segments):
                last = _last_record(path)
                if last is not None:
                    break
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

    def _open_segment(self) -> None:
        now = self._clock()
        self._segment = _segment_id(now)
        self._path = self._dir / f"{self._segment}{SEGMENT_SUFFIX}"
        self._write(AuditEvent(
            type="audit.segment_open", actor="host", action="audit.segment_open",
            outcome="success", sensitivity="security", ts_event=_utc_iso(now),
            payload={"segment": self._segment},
        ), producer="audit")

    def append(self, event: AuditEvent, *, producer: str) -> dict[str, Any]:
        """Seal `event` onto the chain and write it. Returns the record."""
        event = redact(event)
        with self._lock:
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
        return record


__all__ = [
    "AuditJournal",
    "AuditRedactionError",
    "SEGMENT_SUFFIX",
    "redact",
]
