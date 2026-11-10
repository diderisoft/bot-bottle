"""The orchestrator's store manager: migrates every table in its
`registry.db`. The orchestrator is that file's only opener, on every backend;
the host DB (`bot-bottle.db`) is the host controller's and never holds these
tables.
"""

from __future__ import annotations

from pathlib import Path

from ...lib.db_store import DbStore
from .audit_store import AuditStore
from .canary_store import CanaryStore
from .config_store import OrchestratorConfigStore
from .queue_store import QueueStore
from .registry_store import RegistryStore


class StoreManager:
    """Runs migrate/is_migrated across the orchestrator's stores at `db_path`."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def stores(self) -> list[DbStore]:
        return [RegistryStore(self.db_path), QueueStore("", self.db_path),
                AuditStore(self.db_path), CanaryStore(self.db_path),
                OrchestratorConfigStore(self.db_path)]

    def is_migrated(self) -> bool:
        return all(store.is_migrated() for store in self.stores())

    def migrate(self) -> None:
        for store in self.stores():
            store.migrate()


__all__ = ["StoreManager"]
