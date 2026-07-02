"""SQLite-backed audit store for supervise (PRD 0013)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .supervise import AuditEntry


def get_supervise_mod() -> object:
    """Lazy import of supervise to avoid a circular-import at module init time.
    Mirrors our own module identity so patches on supervise.bot_bottle_root
    propagate correctly in both flat (sidecar / sys.path-injection tests) and
    package contexts."""
    import sys
    sv_name = "supervise" if __name__ == "audit_store" else "bot_bottle.supervise"
    if sv_name in sys.modules:
        return sys.modules[sv_name]
    try:
        import bot_bottle.supervise as _m
    except ImportError:
        import supervise as _m  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    return _m


# One entry per schema version: _MIGRATIONS[0] brings a fresh DB (user_version=0)
# to version 1, _MIGRATIONS[1] to version 2, and so on.  Add new migrations at
# the end; never edit existing ones.
_MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS supervise_audit_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        bottle_slug TEXT NOT NULL,
        component TEXT NOT NULL,
        operator_action TEXT NOT NULL,
        operator_notes TEXT NOT NULL,
        justification TEXT NOT NULL,
        diff TEXT NOT NULL
    )
    """,
]


class AuditStore:
    """SQLite-backed persistent store for supervise audit entries."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_supervise_mod().host_db_path()  # type: ignore[attr-defined]
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def write_audit_entry(self, entry: AuditEntry) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO supervise_audit_entries (
                    timestamp, bottle_slug, component, operator_action,
                    operator_notes, justification, diff
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.timestamp,
                    entry.bottle_slug,
                    entry.component,
                    entry.operator_action,
                    entry.operator_notes,
                    entry.justification,
                    entry.diff,
                ),
            )
        self._chmod()
        return self.db_path

    def read_audit_entries(self, component: str, slug: str) -> list[AuditEntry]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM supervise_audit_entries
                WHERE component = ? AND bottle_slug = ?
                ORDER BY id
                """,
                (component, slug),
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
        m = get_supervise_mod()
        return m.AuditEntry(  # type: ignore[attr-defined]
            timestamp=row["timestamp"],
            bottle_slug=row["bottle_slug"],
            component=row["component"],
            operator_action=row["operator_action"],
            operator_notes=row["operator_notes"],
            justification=row["justification"],
            diff=row["diff"],
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    _SCHEMA_KEY = "audit_store"

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    module TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            row = conn.execute(
                "SELECT version FROM schema_versions WHERE module = ?",
                (self._SCHEMA_KEY,),
            ).fetchone()
            version = row[0] if row else 0
            for i, sql in enumerate(_MIGRATIONS[version:], start=version + 1):
                conn.execute(sql)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_versions (module, version) VALUES (?, ?)",
                    (self._SCHEMA_KEY, i),
                )
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


__all__ = ["AuditStore"]
