"""The host DB, `<root>/db/bot-bottle.db`: the host's source-of-truth state
(ADR 0020). The host controller is its only writer and migrates it when it
starts, without asking. The orchestrator's tables are in its own `registry.db`
and never here.
"""

from __future__ import annotations

from pathlib import Path

from ...lib.config_store import ConfigStore
from ...lib.db_store import DbStore
from ...paths import host_audit_dir, host_db_path
from .audit_forward import ForwardCursors
from .audit_index import AuditIndex


def stores(db_path: Path | None = None) -> list[DbStore]:
    """Every store with tables in the host DB. The audit forwarder's and the
    audit index's migrate as they open."""
    path = db_path or host_db_path()
    return [ConfigStore(path), ForwardCursors(path), AuditIndex(host_audit_dir(), path)]


def migrate(db_path: Path | None = None) -> None:
    """Bring every host table up to date."""
    for store in stores(db_path):
        store.migrate()


__all__ = ["migrate", "stores"]
