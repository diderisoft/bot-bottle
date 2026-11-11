"""Prune the audit journal past the host's retention (`bot-bottle audit
retention`), which keeps everything unless set.

The prune runs when the host controller starts and daily after. Only whole
segments every reader has finished go: the index is brought up to date first,
and while forwarding is set, nothing the forwarder hasn't sent goes.
`AuditJournal.prune` appends `journal.pruned` before deleting anything.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...lib.config_store import AuditRetention, ConfigStore
from ...paths import host_db_path
from .audit_forward import ForwardCursors
from .audit_index import AuditIndex, IndexBreak
from .index_db import audit_index

if TYPE_CHECKING:
    from .audit_events import HostAudit

PRUNE_SECONDS = 86400.0
GIB = 1024 ** 3


def _retention() -> AuditRetention:
    return ConfigStore(host_db_path()).audit_retention()


def _forwarded() -> str | None:
    """The segment the forwarder resumes in, or None when it isn't
    forwarding."""
    forward = ConfigStore(host_db_path()).audit_forward()
    if forward is None:
        return None
    return ForwardCursors(host_db_path()).get(forward.url).segment


def prune(audit: "HostAudit", retention: AuditRetention, *, index: AuditIndex,
          forwarded: Callable[[], str | None] = _forwarded) -> dict[str, Any] | None:
    """Prune what `retention` allows and every reader has finished; the
    `journal.pruned` record, or None when nothing went. IndexBreak when the
    index can't catch up, so nothing is pruned past what it holds."""
    if retention.keeps_all:
        return None
    index.sync()
    needed = [index.cursor_segment()]
    sent = forwarded()
    if sent is not None:
        needed.append(sent)
    return audit.prune(max_age_days=retention.days, max_bytes=retention.max_gb * GIB,
                       keep_from=min(needed))


class JournalPruner:
    """Prunes at start and every PRUNE_SECONDS until stopped."""

    def __init__(self, audit: "HostAudit", *,
                 retention: Callable[[], AuditRetention] = _retention,
                 index: Callable[[], AuditIndex] = audit_index,
                 interval: float = PRUNE_SECONDS) -> None:
        self._audit = audit
        self._retention = retention
        self._index = index
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audit-retention", daemon=True)

    def start(self) -> "JournalPruner":
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop, waiting for a prune in progress, so it never appends to the
        journal after the host controller closes it."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                pruned = prune(self._audit, self._retention(), index=self._index())
                if pruned is not None:
                    log.info(f"audit: pruned {len(pruned['payload']['segments'])} old "
                             "journal segment(s)")
            except (IndexBreak, OSError, sqlite3.Error, ValueError) as e:
                log.warn(f"audit: journal retention skipped: {e}")
            if self._stop.wait(self._interval):
                return


__all__ = ["GIB", "JournalPruner", "PRUNE_SECONDS", "prune"]
