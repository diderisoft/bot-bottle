"""The index DB, `<root>/db/index.db`: indexes derived from logs (ADR 0020),
today the audit index. The host controller is its only writer.

Everything in it can be rebuilt, so it's never backed up. When the host
controller starts, once its control socket answers, it checks the file in the
background; one missing or failing `PRAGMA quick_check` is deleted and each
index rebuilt from its log. Readers wait for the check. A rebuilt
audit index holds only the journal's kept segments; `journal.pruned` records
say what went before them.

A new derived index, such as a search over transcripts, is a store listed in
`stores()` with a `rebuild()`, never a table in the host DB.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ...lib import log
from ...lib.db_store import drop_unowned, quick_check
from ...paths import host_audit_dir, index_db_path
from .audit_index import AuditIndex, IndexBreak


class DerivedIndex(Protocol):
    """A store in the index DB."""

    @property
    def TABLES(self) -> Mapping[str, str]:  # pylint: disable=invalid-name
        """Each table it creates, marked as `DbStore.TABLES` is. Any other
        table in the index DB is dropped."""
        ...  # pylint: disable=unnecessary-ellipsis

    @property
    def schema_key(self) -> str:
        """Its module in `schema_versions`."""
        ...  # pylint: disable=unnecessary-ellipsis

    def migrate(self) -> None: ...

    def rebuild(self) -> int:
        """Re-create the index from its log. Returns how many entries."""
        ...  # pylint: disable=unnecessary-ellipsis


# Clear while the host controller checks the index DB at start
# (`open_in_background`), which may delete it: a reader waits.
_checked = threading.Event()
_checked.set()
# How long a reader waits for that check before going ahead anyway.
CHECK_WAIT_SECONDS = 300.0


def audit_index(db_path: Path | None = None) -> AuditIndex:
    """The audit journal's index, once the start's check is done."""
    if not _checked.wait(CHECK_WAIT_SECONDS):
        log.warn("index database: still being checked; reading it anyway")
    return AuditIndex(host_audit_dir(), db_path or index_db_path())


def stores(db_path: Path | None = None) -> list[DerivedIndex]:
    """Every store with tables in the index DB. They migrate as they open."""
    return [AuditIndex(host_audit_dir(), db_path or index_db_path())]


def _delete(path: Path) -> None:
    for name in (path.name, f"{path.name}-wal", f"{path.name}-shm"):
        path.with_name(name).unlink(missing_ok=True)


def open_for_start(db_path: Path | None = None) -> bool:
    """The host controller's start: delete the index DB if it fails its
    check, then migrate it, dropping any table no index owns. True when it was missing or deleted, so its
    indexes need a rebuild."""
    path = db_path or index_db_path()
    missing = not path.exists()
    problem = quick_check(path)
    if problem:
        log.warn(f"index database {path} failed its check ({problem}); rebuilding it")
    if missing or problem:
        _delete(path)
    owners = stores(path)
    for store in owners:
        store.migrate()
    drop_unowned(path, [t for s in owners for t in s.TABLES], [s.schema_key for s in owners])
    return missing or bool(problem)


def rebuild(db_path: Path | None = None) -> None:
    """Rebuild every index, logging what fails."""
    for store in stores(db_path):
        try:
            count = store.rebuild()
        except (IndexBreak, OSError, sqlite3.Error, ValueError) as e:
            log.warn(f"index database: {type(store).__name__} not rebuilt: {e}")
        else:
            log.info(f"index database: {type(store).__name__} rebuilt, {count} entries")


def open_in_background(db_path: Path | None = None) -> threading.Thread:
    """The host controller's start: `open_for_start`, then `rebuild` if it
    says so, off the start's path. A large index DB takes a while to check,
    and the control socket answers meanwhile. Readers of the index
    (`audit_index`) wait for the check, not the rebuild."""
    _checked.clear()

    def run() -> None:
        try:
            rebuilt = open_for_start(db_path)
        except (OSError, sqlite3.Error) as e:
            log.warn(f"index database: not checked: {e}")
            rebuilt = False
        finally:
            _checked.set()
        if rebuilt:
            rebuild(db_path)

    thread = threading.Thread(target=run, daemon=True, name="index-db-open")
    thread.start()
    return thread


def rebuild_in_background(db_path: Path | None = None) -> threading.Thread:
    """`rebuild` off the start's path: a large journal takes a while. The
    audit index serializes its writes, so a query or prune waits for it."""
    thread = threading.Thread(target=rebuild, args=(db_path,), daemon=True,
                              name="index-db-rebuild")
    thread.start()
    return thread


__all__ = ["CHECK_WAIT_SECONDS", "DerivedIndex", "audit_index", "open_for_start",
           "open_in_background", "rebuild",
           "rebuild_in_background", "stores"]
