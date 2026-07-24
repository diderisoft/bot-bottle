"""The orchestrator's store manager (PRD 0013 / 0070).

One store manager per service. Post-PRD-0070 the orchestrator is the sole
opener of `bot-bottle.db`, so it owns migrating every table in it: the supervise
`queue_store` (local to `orchestrator.store`) plus the `audit_store` and
`config_store` from the shared `bot_bottle.store` base. The data plane never
touches these — it reaches state over the control-plane RPC.
"""

from __future__ import annotations

from pathlib import Path

from .queue_store import QueueStore
from ...store.audit_store import AuditStore
from ...store.config_store import ConfigStore

_instance: StoreManager | None = None


class StoreManager:
    """Owns db_path and delegates migrate/is_migrated across the orchestrator's
    stores.

    Use instance() for normal access. Call reset(db_path) in tests to swap the
    singleton to a temp path, then reset() with no args to restore the
    default."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            from ...paths import host_db_path
            db_path = host_db_path()
        self.db_path = db_path

    @classmethod
    def instance(cls) -> StoreManager:
        global _instance
        if _instance is None:
            _instance = cls()
        return _instance

    @classmethod
    def reset(cls, db_path: Path | None = None) -> None:
        """Replace the singleton. Pass db_path for test isolation; omit to restore default."""
        global _instance
        _instance = cls(db_path)

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
