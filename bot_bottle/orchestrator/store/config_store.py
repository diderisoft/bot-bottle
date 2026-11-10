"""Orchestrator settings, in the orchestrator's `registry.db`. A launch's
teardown wait never reads them, only its env override
(`resolve_teardown_timeout`).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations

TEARDOWN_TIMEOUT_ENV = "BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS"
DEFAULT_TEARDOWN_TIMEOUT_SECONDS = 30.0

_MIGRATIONS = TableMigrations(
    "orchestrator_config",
    [
        """
        CREATE TABLE IF NOT EXISTS orchestrator_config (
            id                       INTEGER PRIMARY KEY CHECK (id = 1),
            teardown_timeout_seconds REAL
        )
        """,
    ],
)


class OrchestratorConfigStore(DbStore):
    """Orchestrator settings."""

    TABLES = {"orchestrator_config": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, _MIGRATIONS)

    def get_teardown_timeout_seconds(self) -> float | None:
        """Return the configured teardown timeout, or None if not set."""
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT teardown_timeout_seconds FROM orchestrator_config WHERE id = 1"
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["teardown_timeout_seconds"] if row else None

    def set_teardown_timeout_seconds(self, value: float) -> None:
        """Persist the teardown timeout."""
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO orchestrator_config"
                " (id, teardown_timeout_seconds) VALUES (1, ?)",
                (value,),
            )
        self._chmod()

    def delete_teardown_timeout_seconds(self) -> bool:
        """Clear the stored teardown timeout. Returns True if a value existed."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE orchestrator_config SET teardown_timeout_seconds = NULL"
                " WHERE id = 1 AND teardown_timeout_seconds IS NOT NULL"
            )
        return cur.rowcount > 0


def resolve_teardown_timeout() -> float:
    """How long a launch waits for the orchestrator to tear a bottle down:
    `BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS`, else
    `DEFAULT_TEARDOWN_TIMEOUT_SECONDS` (30 s).
    """
    raw = os.environ.get(TEARDOWN_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TEARDOWN_TIMEOUT_SECONDS


__all__ = [
    "OrchestratorConfigStore",
    "resolve_teardown_timeout",
    "TEARDOWN_TIMEOUT_ENV",
    "DEFAULT_TEARDOWN_TIMEOUT_SECONDS",
]
