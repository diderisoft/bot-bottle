"""Singleton manager for all bot-bottle SQLite stores (PRD 0013)."""

from __future__ import annotations

from pathlib import Path

try:
    from .audit_store import AuditStore
    from .queue_store import QueueStore
except ImportError:
    from audit_store import AuditStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from queue_store import QueueStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module

_instance: StoreManager | None = None


class StoreManager:
    """Owns db_path and delegates migrate/check_migrations across all stores.

    Use instance() for normal access. Call reset(db_path) in tests to swap
    the singleton to a temp path, then reset() with no args to restore the
    default."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            # Lazy import to avoid a circular dependency: supervise imports
            # StoreManager at module level; StoreManager must not import
            # supervise at module level in return.
            try:
                from .supervise import host_db_path
            except ImportError:
                from supervise import host_db_path  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
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

    def check_migrations(self) -> bool:
        return (
            QueueStore("", self.db_path).check_migrations()
            and AuditStore(self.db_path).check_migrations()
        )

    def migrate(self) -> None:
        QueueStore("", self.db_path).migrate()
        AuditStore(self.db_path).migrate()


__all__ = ["StoreManager"]
