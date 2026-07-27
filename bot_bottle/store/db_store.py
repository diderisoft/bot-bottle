"""Shared SQLite-backed store base class for bot-bottle (PRD 0013)."""

from __future__ import annotations

import os
import sqlite3
import stat
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
        self._secure_parent()
        if self.db_path.exists():
            self._chmod()

    def _secure_parent(self) -> None:
        """Create and verify the private parent directory."""
        parent = self.db_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent.chmod(0o700)
        if stat.S_IMODE(parent.stat().st_mode) != 0o700:
            raise PermissionError(f"database directory is not mode 0700: {parent}")

    def _secure_db_file(self) -> None:
        """Create the database without a permissive filesystem window.

        SQLite otherwise creates a missing database using the process umask.
        This store contains control-plane identity tokens, so both creation and
        repair are fail-closed rather than best-effort.
        """
        try:
            fd = os.open(
                self.db_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._chmod()

    def _chmod(self) -> None:
        """Enforce and verify the private database mode after every write."""
        self.db_path.chmod(0o600)
        if stat.S_IMODE(self.db_path.stat().st_mode) != 0o600:
            raise PermissionError(f"database is not mode 0600: {self.db_path}")

    def _connect(self) -> sqlite3.Connection:
        self._secure_db_file()
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
        """Apply any pending migrations to the already-secured DB file."""
        with self._connection() as conn:
            self._migrations.apply(conn)


__all__ = ["DbStore", "DbVersionError"]
