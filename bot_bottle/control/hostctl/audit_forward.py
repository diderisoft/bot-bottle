"""Forward the audit journal to a configured HTTPS endpoint, at least once.

A host-controller thread POSTs journal lines past a persisted per-endpoint
cursor, as native NDJSON or OCSF, advancing it only on a 2xx; the receiver
dedupes on `id`. A new endpoint starts from the beginning. Never blocks the
data plane; each failure streak is recorded as `audit.export_failed`. Only the
local chain is forwarded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...contract.audit import AuditEvent, canonical
from ...lib.config_store import AuditForward
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...secret_source import SecretSource
from .audit_journal import SEGMENT_SUFFIX
from .audit_ocsf import to_ocsf

BATCH_RECORDS = 500
IDLE_SECONDS = 5.0
RETRY_MIN_SECONDS, RETRY_MAX_SECONDS = 2.0, 60.0
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Cursor:
    segment: str = ""
    offset: int = 0


class ForwardCursors(DbStore):
    """Each endpoint's cursor into the journal, in the shared host DB."""

    TABLES = {"audit_forward_cursor": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("audit_forward", [
            """
            CREATE TABLE audit_forward_cursor (
                url TEXT PRIMARY KEY,
                segment TEXT NOT NULL,
                offset INTEGER NOT NULL
            )
            """,
        ]))
        self.migrate()

    def get(self, url: str) -> Cursor:
        with self._connection() as conn:
            row = conn.execute("SELECT segment, offset FROM audit_forward_cursor "
                               "WHERE url = ?", (url,)).fetchone()
        return Cursor(row["segment"], row["offset"]) if row else Cursor()

    def set(self, url: str, cursor: Cursor) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO audit_forward_cursor VALUES (?, ?, ?)",
                         (url, cursor.segment, cursor.offset))
        self._chmod()


def read_batch(journal_dir: Path, cursor: Cursor,
               limit: int = BATCH_RECORDS) -> tuple[list[str], Cursor]:
    """Up to `limit` complete lines past `cursor`, and the new cursor. A torn
    final line waits for the next read."""
    lines: list[str] = []
    for path in sorted(journal_dir.glob(f"*{SEGMENT_SUFFIX}")):
        name = path.name
        if name < cursor.segment:
            continue
        offset = cursor.offset if name == cursor.segment else 0
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n") or len(lines) >= limit:
                return lines, Cursor(name, offset)
            lines.append(raw.decode("utf-8").rstrip("\n"))
            offset += len(raw)
        cursor = Cursor(name, offset)
    return lines, cursor


def batch_body(lines: list[str], fmt: str) -> bytes:
    """NDJSON of `lines`: verbatim, or each as OCSF."""
    if fmt == "ocsf":
        lines = [canonical(to_ocsf(json.loads(line))) for line in lines]
    return ("\n".join(lines) + "\n").encode("utf-8")


def post(forward: AuditForward, body: bytes, token: str) -> None:
    """POST a batch; raises unless 2xx."""
    headers = {"Content-Type": "application/x-ndjson"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(forward.url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
        pass


class AuditForwarder:
    def __init__(self, journal_dir: Path, *, cursors: ForwardCursors,
                 config: Callable[[], AuditForward | None], secrets: SecretSource,
                 record_failure: Callable[[AuditEvent], None],
                 now: Callable[[], str]) -> None:
        self._dir = journal_dir
        self._cursors = cursors
        self._config = config
        self._secrets = secrets
        self._record_failure = record_failure
        self._now = now
        self._stop = threading.Event()
        self._failing = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="audit-forward")

    def start(self) -> "AuditForwarder":
        self._thread.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def forward_once(self) -> int | None:
        """Send one batch. Returns records sent (0 when there's nothing or
        nowhere to send), or None when the send failed."""
        try:
            forward = self._config()
        except sqlite3.Error:
            return 0
        if forward is None:
            return 0
        cursor = self._cursors.get(forward.url)
        lines, after = read_batch(self._dir, cursor)
        if not lines:
            return 0
        token = (self._secrets.get(forward.secret) or "") if forward.secret else ""
        try:
            post(forward, batch_body(lines, forward.format), token)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()
            self._failed(forward, e)
            return None
        self._cursors.set(forward.url, after)
        self._failing = False
        return len(lines)

    def _failed(self, forward: AuditForward, error: Exception) -> None:
        """Record the start of a failure streak, not every retry."""
        if self._failing:
            return
        self._failing = True
        self._record_failure(AuditEvent(
            type="audit.export_failed", actor="hostctl", action="audit.forward",
            outcome="failure", sensitivity="security", ts_event=self._now(),
            resource=forward.url, payload={"detail": str(error)[:200], "url": forward.url},
        ))

    def _run(self) -> None:
        backoff = RETRY_MIN_SECONDS
        while not self._stop.is_set():
            sent = self.forward_once()
            if sent is None:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, RETRY_MAX_SECONDS)
                continue
            backoff = RETRY_MIN_SECONDS
            if sent == 0:
                self._stop.wait(IDLE_SECONDS)


__all__ = ["AuditForwarder", "Cursor", "ForwardCursors", "batch_body", "read_batch"]
