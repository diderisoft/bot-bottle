"""SQLite-backed bot-bottle configuration store."""

from __future__ import annotations

from pathlib import Path

try:
    from .db_store import DbStore
    from .migrations import TableMigrations
    from .supervise_types import host_db_path
except ImportError:
    from db_store import DbStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from migrations import TableMigrations  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from supervise_types import host_db_path  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS = 1


class ConfigStore(DbStore):
    """SQLite configuration for host-side bot-bottle settings."""

    def __init__(self, db_path: Path | None = None) -> None:
        migrations = TableMigrations("config_store", [
            # v1 — host-side bot-bottle settings
            """
            CREATE TABLE IF NOT EXISTS bot_bottle_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cached_image_stale_warning_days INTEGER NOT NULL DEFAULT 1
            )
            """,
        ])
        super().__init__(db_path or host_db_path(), migrations)

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
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
]
