"""Shared SQLite-backed store base class for bot-bottle (PRD 0013)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .migrations import TableMigrations


class DbVersionError(Exception):
    """Raised when the on-disk schema is behind the current migration list."""


class DbStore:
    """Base for SQLite-backed stores. Subclasses resolve db_path then call super().__init__."""

    def __init__(self, db_path: Path, migrations: TableMigrations) -> None:
        self.db_path = db_path
        self._migrations = migrations
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def is_migrated(self) -> bool:
        """Return True if the DB is fully up-to-date, False if migration is needed."""
        if not self.db_path.exists():
            return False
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT version FROM schema_versions WHERE module = ?",
                    (self._migrations.schema_key,),
                ).fetchone()
        except sqlite3.OperationalError:
            return False
        version = row[0] if row else 0
        return version == len(self._migrations.migrations)

    def migrate(self) -> None:
        """Apply any pending migrations and set permissions on the DB file."""
        with self._connection() as conn:
            self._migrations.apply(conn)
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


__all__ = ["DbStore", "DbVersionError"]
