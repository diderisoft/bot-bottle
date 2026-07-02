"""Shared SQLite-backed store base class for bot-bottle (PRD 0013)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from .migrations import TableMigrations
except ImportError:
    from migrations import TableMigrations  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


class DbStore:
    """Base for SQLite-backed stores. Subclasses resolve db_path then call super().__init__."""

    def __init__(self, db_path: Path, migrations: TableMigrations) -> None:
        self.db_path = db_path
        self._migrations = migrations
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            self._migrations.apply(conn)
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


__all__ = ["DbStore"]
