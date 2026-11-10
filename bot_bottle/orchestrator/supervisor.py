"""`Supervisor`: the supervise queue and canary stores at `db_path`, as owned by
`OrchestratorCore`. Types are in `contract.supervise`. The data plane never
imports this.
"""

from __future__ import annotations

from pathlib import Path

from ..contract.supervise import CanaryEvent, Proposal, Response
from .store.canary_store import CanaryStore
from .store.queue_store import QueueStore


class Supervisor:
    """Supervise queue and canary I/O against `db_path`. Decisions are
    recorded only in the audit journal (`decision.resolved`)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _queue(self, queue_key: str) -> QueueStore:
        return QueueStore(queue_key, self._db_path)

    def _canary(self) -> CanaryStore:
        return CanaryStore(self._db_path)

    # --- Queue I/O ---

    def write_proposal(self, proposal: Proposal) -> Path:
        """Store `proposal` (0o600)."""
        return self._queue(proposal.bottle_slug).write_proposal(proposal)

    def read_proposal(self, bottle_slug: str, proposal_id: str) -> Proposal:
        return self._queue(bottle_slug).read_proposal(proposal_id)

    def list_pending_proposals(self, bottle_slug: str) -> list[Proposal]:
        """One bottle's undecided proposals, FIFO."""
        return self._queue(bottle_slug).list_pending_proposals()

    def list_all_pending_proposals(self) -> list[Proposal]:
        """All pending proposals, FIFO."""
        return self._queue("").list_all_pending_proposals()

    def write_response(self, bottle_slug: str, response: Response) -> Path:
        return self._queue(bottle_slug).write_response(response)

    def read_response(self, bottle_slug: str, proposal_id: str) -> Response:
        return self._queue(bottle_slug).read_response(proposal_id)

    def archive_all_proposals(self, bottle_slug: str) -> None:
        """Archive every proposal and response for `bottle_slug`. Idempotent."""
        self._queue(bottle_slug).archive_all()

    # --- Canary events ---

    def write_canary_event(self, event: CanaryEvent) -> None:
        self._canary().write_event(event)

    def read_canary_event(self, event_id: str) -> CanaryEvent:
        return self._canary().read_event(event_id)

    def list_unresolved_canary_events(self) -> list[CanaryEvent]:
        return self._canary().list_unresolved()

    def resolve_canary_event(
        self,
        event_id: str,
        *,
        status: str,
        resolved_at: str,
        notes: str,
    ) -> bool:
        return self._canary().resolve_event(
            event_id, status=status, resolved_at=resolved_at, notes=notes,
        )

    def archive_canary_events(self, bottle_slug: str) -> None:
        """Archive all canary events for `bottle_slug` (teardown/reconcile)."""
        self._canary().archive_all_for_bottle(bottle_slug)
