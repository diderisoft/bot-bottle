"""SQLite store base class, and the one helper every database connection is
opened with (`connect`): WAL, so readers never block the writer, and a busy
timeout, so a writer waits out another's short transaction."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from .migrations import TableMigrations

# How long a connection waits for another's lock before failing.
BUSY_TIMEOUT_MS = 5000

# What each table is (ADR 0020): `PORTABLE` state could be restored onto
# another machine; `MACHINE_SPECIFIC` state couldn't. Each store names its
# tables in `TABLES`. Nothing reads the mark yet.
PORTABLE = "portable"
MACHINE_SPECIFIC = "machine-specific"


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection to the database at `path` in WAL mode with a busy
    timeout. Every store's connections are opened here."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    return conn


def quick_check(path: Path) -> str:
    """The first problem `PRAGMA quick_check` finds in the database at
    `path`, or "" if it passes (or there's no file yet)."""
    if not path.exists():
        return ""
    try:
        conn = connect(path)
    except sqlite3.DatabaseError as e:
        return str(e)
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as e:
        return str(e)
    finally:
        conn.close()
    problems = [str(row[0]) for row in rows if row[0] != "ok"]
    return problems[0] if problems else ""


class DbVersionError(Exception):
    """The on-disk schema is behind the migration list."""


class DbStore:
    """Base for SQLite stores; subclasses resolve db_path, then call
    super().__init__. `TABLES` marks each table the store creates `PORTABLE`
    or `MACHINE_SPECIFIC`."""

    TABLES: ClassVar[Mapping[str, str]] = {}

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
        if parent.is_symlink():
            raise PermissionError(f"database directory must not be a symlink: {parent}")
        parent.chmod(0o700)
        parent_stat = parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError(f"database parent is not a directory: {parent}")
        if stat.S_IMODE(parent_stat.st_mode) != 0o700:
            raise PermissionError(f"database directory is not mode 0700: {parent}")

    def _secure_db_file(self) -> None:
        """Create the database with no permissive window (SQLite would use the
        umask). It holds identity tokens, so creation and repair fail closed.
        """
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(
            self.db_path,
            flags,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            self._secure_open_file(fd)
        finally:
            os.close(fd)

    def _chmod(self) -> None:
        """Enforce and verify the private database mode after every write."""
        fd = os.open(self.db_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            self._secure_open_file(fd)
        finally:
            os.close(fd)

    def _secure_open_file(self, fd: int) -> None:
        """Pin, validate and secure an opened database file."""
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError(
                f"database must be a regular file: {self.db_path}"
            )
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise PermissionError(f"database is not mode 0600: {self.db_path}")

    def _connect(self) -> sqlite3.Connection:
        self._secure_db_file()
        return connect(self.db_path)

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def is_migrated(self) -> bool:
        """True if the DB is up to date."""
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
        """Apply pending migrations."""
        with self._connection() as conn:
            self._migrations.apply(conn)


__all__ = ["BUSY_TIMEOUT_MS", "MACHINE_SPECIFIC", "PORTABLE", "DbStore", "DbVersionError",
           "connect", "quick_check"]
