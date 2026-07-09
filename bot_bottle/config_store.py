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


CACHED_IMAGE_STALE_WARNING_DAYS = "cached_image_stale_warning_days"
DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS = 1


class ConfigStore(DbStore):
    """SQLite key/value configuration for host-side bot-bottle settings."""

    def __init__(self, db_path: Path | None = None) -> None:
        migrations = TableMigrations("config_store", [
            # v1 — generic string key/value settings
            """
            CREATE TABLE IF NOT EXISTS bot_bottle_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ])
        super().__init__(db_path or host_db_path(), migrations)

    def get(self, key: str) -> str | None:
        if not self.db_path.is_file():
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_bottle_config WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set(self, key: str, value: str) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
        self._chmod()
        return self.db_path

    def get_int(self, key: str, default: int) -> int:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def cached_image_stale_warning_days(self) -> int:
        return self.get_int(
            CACHED_IMAGE_STALE_WARNING_DAYS,
            DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
        )


__all__ = [
    "CACHED_IMAGE_STALE_WARNING_DAYS",
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
]
