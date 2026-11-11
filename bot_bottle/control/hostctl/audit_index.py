"""A searchable SQLite index of the audit journal, in the index DB
(`index_db.py`): a derived cache that `rebuild` replays from the journal.

`sync` indexes new records, checking each one's hash and link, so a query never
shows a record that doesn't chain. A break stops indexing; `audit verify` says
where.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contract.audit import record_hash
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from .audit_journal import segments
from .audit_verify import verify


IMPORTED_DIRNAME = "imported"


def imported_chains(journal_dir: Path) -> list[Path]:
    """Each imported chain's directory, `imported/<host>/<chain>/`."""
    root = journal_dir / IMPORTED_DIRNAME
    if not root.is_dir():
        return []
    return sorted(c for host in root.iterdir() if host.is_dir()
                  for c in host.iterdir() if c.is_dir())


def read_chain(directory: Path) -> list[tuple[dict[str, Any], str]]:
    """Every record in a chain directory, with its line as stored."""
    out: list[tuple[dict[str, Any], str]] = []
    for path in segments(directory):
        for raw in path.read_text(encoding="utf-8").splitlines():
            out.append((json.loads(raw), raw))
    return out

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


# One writer at a time in this process: a start's rebuild, a query's sync and
# the pruner's would otherwise read the same cursor and race for the lock.
_WRITING = threading.RLock()


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
    """The journal's index. Derived from this host's journal, so it never
    travels."""

    TABLES = {"audit_events": MACHINE_SPECIFIC, "audit_index_cursor": MACHINE_SPECIFIC}

    def __init__(self, journal_dir: Path, db_path: Path) -> None:
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
            # Where `sync` resumes: segment, byte offset, and the last indexed
            # hash.
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
        super().__init__(db_path, migrations)
        self.migrate()

    def _cursor(self, conn: sqlite3.Connection) -> tuple[str, int, str]:
        row = conn.execute(
            "SELECT segment, offset, head FROM audit_index_cursor").fetchone()
        return (row["segment"], row["offset"], row["head"]) if row else ("", 0, "")

    def cursor_segment(self) -> str:
        """The segment file `sync` resumes in; every one before it is fully
        indexed. "" before the first sync."""
        with self._connection() as conn:
            return self._cursor(conn)[0]

    def sync(self) -> int:
        """Index records appended since the last sync. Returns how many."""
        added = 0
        with _WRITING, self._connection() as conn:
            segment, offset, head = self._cursor(conn)
            first = True
            for path in segments(self._journal_dir):
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
        """Drop every row and replay the journal and imported chains. Refuses
        if any is broken."""
        chains = imported_chains(self._journal_dir)
        for directory in [self._journal_dir, *chains]:
            if not verify(directory).ok:
                raise IndexBreak(f"the chain in {directory} is broken; run `audit verify`")
        with _WRITING:
            with self._connection() as conn:
                conn.execute("DELETE FROM audit_events")
                conn.execute("DELETE FROM audit_index_cursor")
            count = self.sync()
            for directory in chains:
                count += self.add_records(read_chain(directory))
        return count

    def add_records(self, records: list[tuple[dict[str, Any], str]]) -> int:
        """Index verified records from outside the local journal, all or none.
        Known ids are skipped; a clash on (host, epoch, seq) is refused."""
        with _WRITING:
            before = self._count()
            try:
                with self._connection() as conn:
                    for record, raw in records:
                        self._insert(conn, record, raw)
            except sqlite3.IntegrityError as e:
                raise IndexBreak(f"a record collides with one already indexed: {e}") from e
            self._chmod()
            return self._count() - before

    def _count(self) -> int:
        with self._connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

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


def query_host_journal(bottle: str, activation: str | None,
                       limit: int) -> list[dict[str, Any]]:
    """`audit query --bottle --activation --limit` on this host's journal;
    empty without one."""
    # pylint: disable=import-outside-toplevel
    from ...paths import host_audit_dir
    from .index_db import audit_index
    if not host_audit_dir().is_dir():
        return []
    index = audit_index()
    index.sync()
    return index.query(Query(bottle=bottle, activation=activation, limit=limit))


__all__ = [
    "query_host_journal",
    "AuditIndex",
    "IMPORTED_DIRNAME",
    "IndexBreak",
    "Query",
    "imported_chains",
    "read_chain",
]
