"""The orchestrator's view of the supervise plane.

Bundles the orchestrator-owned supervise surface: the queue/audit I/O and diff
rendering (`queue`), and the `Supervisor` lifecycle (host-side database
staging). For convenience it re-exports the neutral vocabulary
(`bot_bottle.supervisor`'s types + `SupervisePlan`) so orchestrator-side
callers — service, CLI, tests — import from one place.

The data plane must NOT import this package; it imports `bot_bottle.supervisor`
(neutral) directly and reaches the queue over the control-plane RPC.
"""

from __future__ import annotations

from abc import ABC
from pathlib import Path

from ...supervisor.types import *  # noqa: F401,F403 — re-export the neutral vocabulary
from ...supervisor.plan import SupervisePlan
from ..store.store_manager import StoreManager
from .queue import (
    archive_all_proposals,
    archive_proposal,
    audit_dir,
    audit_log_path,
    list_all_pending_proposals,
    list_pending_proposals,
    read_audit_entries,
    read_proposal,
    read_response,
    render_diff,
    sha256_hex,
    wait_for_response,
    write_audit_entry,
    write_proposal,
    write_response,
)


class Supervisor(ABC):
    """Per-bottle supervise lifecycle. Encapsulates host-side database staging;
    the gateway's start/stop lifecycle is backend-specific.

    `prepare` migrates the orchestrator's `bot-bottle.db` and returns the
    neutral `SupervisePlan`. It touches the orchestrator store manager, so it
    lives here rather than in the neutral `bot_bottle.supervisor` package; the
    backend (which drives launch) may import it — backend → orchestrator is an
    allowed direction, unlike gateway → orchestrator."""

    def prepare(self, slug: str, stage_dir: Path) -> SupervisePlan:
        """Stage the host database. Returns the plan; `internal_network` must be
        set by the launch step before .start runs."""
        del stage_dir
        mgr = StoreManager.instance()
        mgr.migrate()
        return SupervisePlan(slug=slug, db_path=mgr.db_path)
