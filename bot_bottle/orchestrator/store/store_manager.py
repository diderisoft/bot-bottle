"""The orchestrator's store manager: as sole opener of `bot-bottle.db` it
migrates every table in it.
"""

from __future__ import annotations

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


__all__ = ["StoreManager"]
