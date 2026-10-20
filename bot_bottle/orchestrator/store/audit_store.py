"""SQLite-backed audit store for supervise (PRD 0013)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...contract.supervise import AuditEntry
from ...lib.db_store import DbStore
from ...lib.migrations import TableMigrations


class AuditStore(DbStore):
    """SQLite-backed persistent store for supervise audit entries."""

    def __init__(self, db_path: Path) -> None:
        # One entry per schema version: migrations[0] brings a fresh DB to
        # version 1, [1] to version 2, etc.  Add new entries at the end; never
        # edit existing ones.
        migrations = TableMigrations("audit_store", [
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
        ])
        super().__init__(db_path, migrations)

    def write_audit_entry(self, entry: AuditEntry) -> Path:
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        return AuditEntry(
            timestamp=row["timestamp"],
            bottle_slug=row["bottle_slug"],
            component=row["component"],
            operator_action=row["operator_action"],
            operator_notes=row["operator_notes"],
            justification=row["justification"],
            diff=row["diff"],
        )


__all__ = ["AuditStore"]
