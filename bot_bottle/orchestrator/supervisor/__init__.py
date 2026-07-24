"""The orchestrator's supervise service.

`Supervisor` is the service object the `Orchestrator` owns and calls: it wraps
the supervise queue + audit stores (queue proposals, record operator responses,
write audit entries, stage the DB at launch). Binding those operations to one
object — optionally scoped to a `db_path` — makes the orchestrator's supervise
dependency explicit and injectable in tests.

For convenience this package also re-exports the neutral vocabulary
(`bot_bottle.supervisor`'s types + `SupervisePlan`) and the pure `render_diff`
/ `sha256_hex` helpers (`util`), so orchestrator-side callers import from one
place. The data plane must NOT import this package — it imports
`bot_bottle.supervisor` (neutral) directly and reaches the queue over the
control-plane RPC.
"""

from __future__ import annotations

from pathlib import Path

from ...supervisor.types import *  # noqa: F401,F403 — re-export the neutral vocabulary
from ...supervisor.plan import SupervisePlan
from ..store.store_manager import StoreManager
from ..store.queue_store import QueueStore
from ...store.audit_store import AuditStore
from ...supervisor.types import AuditEntry, Proposal, Response
from .util import render_diff, sha256_hex  # noqa: F401 — re-exported for callers


class Supervisor:
    """Host-side supervise service: the queue + audit I/O and launch-time DB
    staging the orchestrator drives. Stateless apart from an optional `db_path`
    (None → the host DB, resolved per store as before), so a test can point a
    `Supervisor` at a temp database."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def _queue(self, queue_key: str) -> QueueStore:
        return QueueStore(queue_key, self._db_path)

    def _audit(self) -> AuditStore:
        return AuditStore(self._db_path)

    # --- Queue I/O ---------------------------------------------------------

    def write_proposal(self, proposal: Proposal) -> Path:
        """Persist `proposal` in the queue database, mode 0o600."""
        return self._queue(proposal.bottle_slug).write_proposal(proposal)

    def read_proposal(self, bottle_slug: str, proposal_id: str) -> Proposal:
        return self._queue(bottle_slug).read_proposal(proposal_id)

    def list_pending_proposals(self, bottle_slug: str) -> list[Proposal]:
        """A single bottle's undecided proposals, FIFO by arrival."""
        return self._queue(bottle_slug).list_pending_proposals()

    def list_all_pending_proposals(self) -> list[Proposal]:
        """All pending proposals across bottles, sorted FIFO."""
        return self._queue("").list_all_pending_proposals()

    def write_response(self, bottle_slug: str, response: Response) -> Path:
        return self._queue(bottle_slug).write_response(response)

    def read_response(self, bottle_slug: str, proposal_id: str) -> Response:
        return self._queue(bottle_slug).read_response(proposal_id)

    def archive_all_proposals(self, bottle_slug: str) -> None:
        """Archive every proposal + response for `bottle_slug` (bottle teardown
        / reconcile cleanup). Idempotent."""
        self._queue(bottle_slug).archive_all()

    # --- Audit log ---------------------------------------------------------

    def write_audit_entry(self, entry: AuditEntry) -> Path:
        """Append `entry` to the host supervise audit table."""
        return self._audit().write_audit_entry(entry)

    def read_audit_entries(self, component: str, slug: str) -> list[AuditEntry]:
        """Load all audit entries for the given component+slug."""
        return self._audit().read_audit_entries(component, slug)

    # --- Launch-time staging ----------------------------------------------

    def prepare(self, slug: str, stage_dir: Path) -> SupervisePlan:
        """Stage the host database and return the plan. Called by the backend at
        launch; `internal_network` is set by the launch step before `.start`."""
        del stage_dir
        mgr = StoreManager(self._db_path) if self._db_path else StoreManager.instance()
        mgr.migrate()
        return SupervisePlan(slug=slug, db_path=mgr.db_path)
