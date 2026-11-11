"""SQLite store base class, and the one helper every database connection is
opened with (`connect`): WAL, so readers never block the writer, and a busy
timeout, so a writer waits out another's short transaction."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator, Iterable, Mapping
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


@contextmanager
def immediate(conn: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """One transaction on `conn` that holds the write lock from its start
    (`BEGIN IMMEDIATE`), committed when the block ends and rolled back if it
    raises. For a read-then-write: in sqlite3's default mode a SELECT opens no
    transaction, so another writer could change what was read before the
    write."""
    level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = level


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


def drop_unowned(path: Path, tables: Iterable[str], modules: Iterable[str]) -> list[str]:
    """Make the database at `path` hold only what its stores own: drop every
    table not in `tables` (with its indexes and triggers), and the
    `schema_versions` rows of modules not in `modules`. VACUUMs once if a
    table was dropped, so the freed pages (and what was in them) go too.
    Returns the tables dropped, sorted."""
    if not path.exists():
        return []
    owned = {"schema_versions", *tables}
    modules = sorted(set(modules))
    conn = connect(path)
    try:
        with immediate(conn):
            tables = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")]
            dropped = sorted(t for t in tables if t not in owned and not t.startswith("sqlite_"))
            for table in dropped:
                conn.execute(f'DROP TABLE "{table.replace(chr(34), chr(34) * 2)}"')
            if "schema_versions" in tables:
                conn.execute("DELETE FROM schema_versions WHERE module NOT IN "
                             f"({', '.join('?' for _ in modules)})", modules)
        if dropped:
            conn.execute("VACUUM")
    finally:
        conn.close()
    return dropped


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

    @contextmanager
    def _write_connection(self) -> Generator[sqlite3.Connection]:
        """A connection in one `immediate` transaction, for a method that
        reads and then writes what it read."""
        conn = self._connect()
        try:
            with immediate(conn):
                yield conn
        finally:
            conn.close()

    @property
    def schema_key(self) -> str:
        """The module name its migrations are recorded under in
        `schema_versions`."""
        return self._migrations.schema_key

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
           "connect", "drop_unowned", "immediate", "quick_check"]
