"""The orchestrator's store manager.

The orchestrator is the sole opener of `bot-bottle.db`, so it owns migrating
every table in it: the supervise `queue_store` and `audit_store` plus the host
settings `config_store`. The data plane never touches these; it reaches state
over the control-plane RPC.
"""

from __future__ import annotations

from pathlib import Path

from .audit_store import AuditStore
from .queue_store import QueueStore
from ...lib.config_store import ConfigStore


class StoreManager:
    """Owns db_path and delegates migrate/is_migrated across the orchestrator's
    stores."""

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
