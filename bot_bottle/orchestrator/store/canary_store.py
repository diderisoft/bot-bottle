"""SQLite store for canary events."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...contract.supervise import (
    CANARY_STATUS_UNRESOLVED,
    CanaryEvent,
)
from ...lib.db_store import DbStore
from ...lib.migrations import TableMigrations


class CanaryStore(DbStore):

    def __init__(self, db_path: Path) -> None:
        migrations = TableMigrations("canary_store", [
            # v1: canary events
            """
            CREATE TABLE IF NOT EXISTS canary_events (
                id TEXT NOT NULL PRIMARY KEY,
                bottle_slug TEXT NOT NULL,
                arrival_timestamp TEXT NOT NULL,
                reason TEXT NOT NULL,
                request_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unresolved',
                resolved_at TEXT NOT NULL DEFAULT '',
                resolution_notes TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0
            )
            """,
        ])
        super().__init__(db_path, migrations)

    def write_event(self, event: CanaryEvent) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO canary_events (
                    id, bottle_slug, arrival_timestamp, reason, request_path,
                    status, resolved_at, resolution_notes, archived
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    event.id,
                    event.bottle_slug,
                    event.arrival_timestamp,
                    event.reason,
                    event.request_path,
                    event.status,
                    event.resolved_at,
                    event.resolution_notes,
                ),
            )
        self._chmod()

    def read_event(self, event_id: str) -> CanaryEvent:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM canary_events WHERE id = ? AND archived = 0",
                (event_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(event_id)
        return self._row_to_event(row)

    def list_unresolved(self) -> list[CanaryEvent]:
        if not self.db_path.is_file():
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM canary_events
                WHERE status = ? AND archived = 0
                ORDER BY arrival_timestamp, id
                """,
                (CANARY_STATUS_UNRESOLVED,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def resolve_event(
        self,
        event_id: str,
        *,
        status: str,
        resolved_at: str,
        notes: str,
    ) -> bool:
        """Resolve an unresolved event. Returns False if not found or already
        resolved."""
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE canary_events
                SET status = ?, resolved_at = ?, resolution_notes = ?
                WHERE id = ? AND status = ? AND archived = 0
                """,
                (status, resolved_at, notes, event_id, CANARY_STATUS_UNRESOLVED),
            )
        self._chmod()
        return cursor.rowcount > 0

    def archive_all_for_bottle(self, bottle_slug: str) -> None:
        """Archive every canary event for `bottle_slug` (bottle teardown or
        reconcile). Idempotent."""
        if not self.db_path.is_file():
            return
        with self._connection() as conn:
            conn.execute(
                "UPDATE canary_events SET archived = 1 WHERE bottle_slug = ?",
                (bottle_slug,),
            )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> CanaryEvent:
        return CanaryEvent(
            id=row["id"],
            bottle_slug=row["bottle_slug"],
            arrival_timestamp=row["arrival_timestamp"],
            reason=row["reason"],
            request_path=row["request_path"],
            status=row["status"],
            resolved_at=row["resolved_at"],
            resolution_notes=row["resolution_notes"],
        )


__all__ = ["CanaryStore"]
