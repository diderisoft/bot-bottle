"""A searchable SQLite index of the audit journal.

A derived cache, not a second source of truth: every row comes from a journal
line, and `rebuild` throws the rows away and replays the journal. It lives
next to the journal rather than in the shared host DB, whose directory is
mounted into the gateway: a gateway that could write here could make `audit
query` show something the journal doesn't say.

`sync` indexes whatever the writer appended since the last call, checking
each new record's hash and its link to the last indexed one, so a query never
shows a record that doesn't chain. A break stops indexing; `audit verify`
then says where.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contract.audit import record_hash
from ...lib.db_store import DbStore
from ...lib.migrations import TableMigrations
from .audit_journal import SEGMENT_SUFFIX
from .audit_verify import verify


INDEX_FILENAME = "index.db"

# Columns copied straight from a record.
_FIELDS = (
    "id", "host", "epoch", "seq", "segment", "type", "ts_event", "ts_recorded",
    "bottle", "bottled_agent", "activation", "actor", "outcome", "sensitivity",
    "correlation_id", "causation_id",
)
# Promoted out of `payload` for forge and commit events.
_PAYLOAD_FIELDS = ("repository", "commit_sha")

_INDEXED = (
    "ts_event", "ts_recorded", "type", "host", "bottle", "bottled_agent",
    "activation", "actor", "outcome", "sensitivity", "correlation_id",
    "causation_id", "repository", "commit_sha",
)


class IndexBreak(Exception):
    """A new journal record doesn't chain onto the last indexed one."""


@dataclass(frozen=True)
class Query:
    since: str | None = None
    until: str | None = None
    type: str | None = None  # exact, or a `group.*` glob
    host: str | None = None
    bottle: str | None = None
    activation: str | None = None
    bottled_agent: str | None = None
    actor: str | None = None
    outcome: str | None = None
    repository: str | None = None
    correlation_id: str | None = None
    commit_sha: str | None = None
    limit: int | None = None


class AuditIndex(DbStore):
    def __init__(self, journal_dir: Path) -> None:
        self._journal_dir = journal_dir
        migrations = TableMigrations("audit_index", [
            f"""
            CREATE TABLE audit_events (
                ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                {", ".join(f"{name} {'INTEGER' if name in ('epoch', 'seq') else 'TEXT'}"
                           for name in _FIELDS + _PAYLOAD_FIELDS)},
                raw TEXT NOT NULL,
                UNIQUE (id),
                UNIQUE (host, epoch, seq)
            )
            """,
            # Where `sync` resumes: the segment, its byte offset, and the hash
            # of the last record indexed.
            """
            CREATE TABLE audit_index_cursor (
                one INTEGER PRIMARY KEY CHECK (one = 1),
                segment TEXT NOT NULL,
                offset INTEGER NOT NULL,
                head TEXT NOT NULL
            )
            """,
            *(f"CREATE INDEX audit_events_{name} ON audit_events ({name})"
              for name in _INDEXED),
        ])
        super().__init__(journal_dir / INDEX_FILENAME, migrations)
        self.migrate()

    def _cursor(self, conn: sqlite3.Connection) -> tuple[str, int, str]:
        row = conn.execute(
            "SELECT segment, offset, head FROM audit_index_cursor").fetchone()
        return (row["segment"], row["offset"], row["head"]) if row else ("", 0, "")

    def sync(self) -> int:
        """Index records appended since the last sync. Returns how many."""
        added = 0
        with self._connection() as conn:
            segment, offset, head = self._cursor(conn)
            first = True
            for path in sorted(self._journal_dir.glob(f"*{SEGMENT_SUFFIX}")):
                name = path.name
                if name < segment:
                    continue
                start = offset if name == segment else 0
                with path.open("rb") as f:
                    f.seek(start)
                    data = f.read()
                complete = data[:data.rfind(b"\n") + 1]  # never a torn tail
                for raw in complete.splitlines():
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise IndexBreak(f"{name}: not a JSON record")
                    if (head or not first) and record.get("prev") != head:
                        raise IndexBreak(f"{name}: record {record.get('id')} "
                                         f"does not chain onto the last indexed record")
                    if record_hash(str(record.get("prev", "")), record) != record.get("hash"):
                        raise IndexBreak(f"{name}: record {record.get('id')} "
                                         f"does not match its hash")
                    self._insert(conn, record, raw.decode("utf-8"))
                    head = str(record["hash"])
                    first = False
                    added += 1
                segment, offset = name, start + len(complete)
            if segment:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_index_cursor VALUES (1, ?, ?, ?)",
                    (segment, offset, head))
        self._chmod()
        return added

    def rebuild(self) -> int:
        """Drop every row and replay the journal. Refuses a broken journal."""
        report = verify(self._journal_dir)
        if not report.ok:
            raise IndexBreak("the journal's chain is broken; run `audit verify`")
        with self._connection() as conn:
            conn.execute("DELETE FROM audit_events")
            conn.execute("DELETE FROM audit_index_cursor")
        return self.sync()

    @staticmethod
    def _insert(conn: sqlite3.Connection, record: dict[str, Any], raw: str) -> None:
        payload = record.get("payload") or {}
        values = [record.get(name) for name in _FIELDS]
        values += [payload.get(name) if isinstance(payload, dict) else None
                   for name in _PAYLOAD_FIELDS]
        columns = _FIELDS + _PAYLOAD_FIELDS
        conn.execute(
            f"INSERT INTO audit_events ({', '.join(columns)}, raw) "
            f"VALUES ({', '.join('?' * (len(columns) + 1))}) "
            "ON CONFLICT (id) DO NOTHING",
            (*values, raw),
        )

    def query(self, q: Query) -> list[dict[str, Any]]:
        """Matching records, in the order they were indexed."""
        where: list[str] = []
        args: list[Any] = []
        if q.since:
            where.append("ts_event >= ?")
            args.append(q.since)
        if q.until:
            where.append("ts_event < ?")
            args.append(q.until)
        if q.type:
            if q.type.endswith(".*"):
                where.append("type LIKE ? ESCAPE '\\'")
                args.append(q.type[:-1].replace("_", "\\_").replace("%", "\\%") + "%")
            else:
                where.append("type = ?")
                args.append(q.type)
        for name in ("host", "bottle", "activation", "bottled_agent", "actor",
                     "outcome", "repository", "correlation_id", "commit_sha"):
            value = getattr(q, name)
            if value is not None:
                where.append(f"{name} = ?")
                args.append(value)
        sql = "SELECT raw FROM audit_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ingest_seq"
        if q.limit is not None:
            sql += " LIMIT ?"
            args.append(q.limit)
        with self._connection() as conn:
            return [json.loads(row["raw"]) for row in conn.execute(sql, args)]


__all__ = ["AuditIndex", "INDEX_FILENAME", "IndexBreak", "Query"]
