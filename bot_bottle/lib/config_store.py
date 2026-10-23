"""SQLite-backed bot-bottle configuration store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db_store import DbStore
from .migrations import TableMigrations


DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS = 1
# What a bottle that doesn't set `audit.delivery` gets.
DEFAULT_AUDIT_DELIVERY = "best_effort"


class ConfigStore(DbStore):
    """SQLite configuration for host-side bot-bottle settings."""

    def __init__(self, db_path: Path) -> None:
        migrations = TableMigrations("config_store", [
            # v1 — host-side bot-bottle settings
            """
            CREATE TABLE IF NOT EXISTS bot_bottle_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cached_image_stale_warning_days INTEGER NOT NULL DEFAULT 1
            )
            """,
            # v2 — the host-wide audit delivery default
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_delivery TEXT NOT NULL DEFAULT 'best_effort'
            """,
        ])
        super().__init__(db_path, migrations)

    def cached_image_stale_warning_days(self) -> int:
        if not self.db_path.is_file():
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cached_image_stale_warning_days
                FROM bot_bottle_config
                WHERE id = 1
                """,
            ).fetchone()
        if row is None:
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
        try:
            return int(row["cached_image_stale_warning_days"])
        except (TypeError, ValueError):
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS

    def audit_delivery(self) -> str:
        """The host-wide `audit.delivery` default."""
        if not self.db_path.is_file():
            return DEFAULT_AUDIT_DELIVERY
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT audit_delivery FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return DEFAULT_AUDIT_DELIVERY  # not migrated yet
        return str(row["audit_delivery"]) if row is not None else DEFAULT_AUDIT_DELIVERY

    def set_audit_delivery(self, delivery: str) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, audit_delivery)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET audit_delivery = excluded.audit_delivery
                """,
                (delivery,),
            )
        self._chmod()
        return self.db_path

    def set_cached_image_stale_warning_days(self, days: int) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, cached_image_stale_warning_days)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cached_image_stale_warning_days = excluded.cached_image_stale_warning_days
                """,
                (days,),
            )
        self._chmod()
        return self.db_path


__all__ = [
    "DEFAULT_AUDIT_DELIVERY",
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
]
