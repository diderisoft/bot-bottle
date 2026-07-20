"""Per-host orchestrator configuration store (key/value settings in bot-bottle.db).

Co-tenants the shared `bot-bottle.db` via the `DbStore` framework.  Settings
are readable by the host launch path directly (no HTTP round-trip to the
orchestrator), so they take effect even before the orchestrator is reachable.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ..db_store import DbStore
from ..migrations import TableMigrations
from ..paths import host_db_path

TEARDOWN_TIMEOUT_KEY = "teardown_timeout_seconds"
TEARDOWN_TIMEOUT_ENV = "BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS"
DEFAULT_TEARDOWN_TIMEOUT_SECONDS = 30.0

_MIGRATIONS = TableMigrations(
    "orchestrator_config",
    [
        """
        CREATE TABLE IF NOT EXISTS orchestrator_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ],
)


class OrchestratorConfigStore(DbStore):
    """Key/value store for orchestrator settings in the shared host DB."""

    def __init__(self, db_path: Path | None = None) -> None:
        super().__init__(db_path or host_db_path(), _MIGRATIONS)

    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get(self, key: str) -> str | None:
        """Return the value for `key`, or None if absent."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT value FROM orchestrator_config WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        """Upsert a setting."""
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO orchestrator_config (key, value) VALUES (?, ?)",
                (key, value),
            )
        self._chmod()

    def delete(self, key: str) -> bool:
        """Remove a setting. Returns True if the key existed."""
        with self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM orchestrator_config WHERE key = ?", (key,)
            )
        return cur.rowcount > 0


def resolve_teardown_timeout(db_path: Path | None = None) -> float:
    """Return the teardown timeout to use, in priority order:

    1. ``BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS`` env var
    2. ``teardown_timeout_seconds`` key in the orchestrator config DB
    3. ``DEFAULT_TEARDOWN_TIMEOUT_SECONDS`` (30 s)
    """
    raw = os.environ.get(TEARDOWN_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass

    store = OrchestratorConfigStore(db_path)
    if not store.is_migrated():
        store.migrate()
    raw_db = store.get(TEARDOWN_TIMEOUT_KEY)
    if raw_db is not None:
        try:
            value = float(raw_db)
            if value > 0:
                return value
        except ValueError:
            pass

    return DEFAULT_TEARDOWN_TIMEOUT_SECONDS


__all__ = [
    "OrchestratorConfigStore",
    "resolve_teardown_timeout",
    "TEARDOWN_TIMEOUT_KEY",
    "TEARDOWN_TIMEOUT_ENV",
    "DEFAULT_TEARDOWN_TIMEOUT_SECONDS",
]
