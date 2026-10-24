"""`Supervisor`: the supervise queue and audit stores at `db_path`, as owned by
`OrchestratorCore`. Types are in `contract.supervise`. The data plane never
imports this.
"""

from __future__ import annotations

from pathlib import Path

from ..contract.supervise import AuditEntry, Proposal, Response
from .store.audit_store import AuditStore
from .store.queue_store import QueueStore


class Supervisor:
    """Supervise queue and audit I/O against `db_path`."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _queue(self, queue_key: str) -> QueueStore:
        return QueueStore(queue_key, self._db_path)

    def _audit(self) -> AuditStore:
        return AuditStore(self._db_path)

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

    # --- Audit log ---

    def write_audit_entry(self, entry: AuditEntry) -> Path:
        """Append `entry` to the host supervise audit table."""
        return self._audit().write_audit_entry(entry)

    def read_audit_entries(self, component: str, slug: str) -> list[AuditEntry]:
        """All audit entries for the component and slug."""
        return self._audit().read_audit_entries(component, slug)
