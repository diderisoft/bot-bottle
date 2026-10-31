"""The orchestrator's store manager: as sole opener of `bot-bottle.db` it
migrates every table in it.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from .audit_store import AuditStore
from .queue_store import QueueStore
from ...lib.config_store import ConfigStore


class StoreManager:
    """Runs migrate/is_migrated across the orchestrator's stores at `db_path`."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def is_migrated(self) -> bool:
        return (
            QueueStore("", self.db_path).is_migrated()
            and AuditStore(self.db_path).is_migrated()
            and ConfigStore(self.db_path).is_migrated()
        )

    def migrate(self) -> None:
        QueueStore("", self.db_path).migrate()
        AuditStore(self.db_path).migrate()
        ConfigStore(self.db_path).migrate()

    def is_fresh(self) -> bool:
        """Whether the DB is missing or holds no tables yet: a new install or
        environment, with nothing in it to migrate. Reads without creating
        the file."""
        if not self.db_path.exists():
            return True
        try:
            with contextlib.closing(
                    sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)) as db:
                return db.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                                  ).fetchone()[0] == 0
        except sqlite3.Error:
            return False

    def migrate_if_fresh(self) -> bool:
        """Create a fresh DB's tables (no question to ask: there's nothing to
        migrate from). True if it did."""
        if not self.is_fresh():
            return False
        self.migrate()
        return True


__all__ = ["StoreManager"]
