"""Shared SQLite-backed store base class for bot-bottle (PRD 0013)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from .migrations import TableMigrations
except ImportError:
    from migrations import TableMigrations  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


# The single shared host state DB. All bot-bottle SQLite stores (supervise
# queue, audit, the orchestrator registry) co-tenant this one file — the
# TableMigrations schema_key namespaces each store's tables.
HOST_DB_FILENAME = "bot-bottle.db"


def host_db_path() -> Path:
    """Path to the shared host state DB, `<bot_bottle_root>/db/bot-bottle.db`.

    Kept in its own `db/` subdirectory (not directly under the root) so a
    backend that can only bind-mount *directories* can share this one file
    with a sidecar without exposing the root's other contents (git-gate
    keys, per-bottle state, ...).

    Resolves `bot_bottle_root` from `supervise_types` at call time (a lazy
    import — avoids a load-time cycle, and lets a monkey-patch of
    `supervise_types.bot_bottle_root` propagate here)."""
    try:
        from . import supervise_types as _st
    except ImportError:  # flat imports inside the sidecar bundle
        import supervise_types as _st  # type: ignore[import-not-found,no-redef]  # pylint: disable=import-error,no-name-in-module
    return _st.bot_bottle_root() / "db" / HOST_DB_FILENAME


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

    def is_migrated(self) -> bool:
        """Return True if the DB is fully up-to-date, False if migration is needed."""
        if not self.db_path.exists():
            return False
        try:
            with self._connect() as conn:
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
        with self._connect() as conn:
            self._migrations.apply(conn)
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


__all__ = ["DbStore", "DbVersionError", "HOST_DB_FILENAME", "host_db_path"]
