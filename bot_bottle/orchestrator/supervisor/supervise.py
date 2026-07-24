"""The `Supervise` lifecycle — host-side database staging (PRD 0013 / 0070).

`Supervise.prepare` migrates the orchestrator's `bot-bottle.db` and returns the
neutral `SupervisePlan`. It touches the orchestrator store manager, so it lives
here rather than in the neutral `bot_bottle.supervisor` package; the backend
(which drives launch) may import it — backend → orchestrator is an allowed
direction, unlike gateway → orchestrator.
"""

from __future__ import annotations

from abc import ABC
from pathlib import Path

from ..store.store_manager import StoreManager
from ...supervisor.plan import SupervisePlan


class Supervise(ABC):
    """Per-bottle supervise daemon. Encapsulates host-side database staging;
    the gateway's start/stop lifecycle is backend-specific."""

    def prepare(
        self,
        slug: str,
        stage_dir: Path,
    ) -> SupervisePlan:
        """Stage the host database. Returns the plan; `internal_network` must be
        set by the launch step before .start runs."""
        del stage_dir
        mgr = StoreManager.instance()
        mgr.migrate()
        return SupervisePlan(
            slug=slug,
            db_path=mgr.db_path,
        )
