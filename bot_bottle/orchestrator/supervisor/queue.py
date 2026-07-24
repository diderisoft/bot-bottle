"""Orchestrator-side supervise queue + audit I/O and diff rendering.

The host-side library the orchestrator uses to drive the supervise flow: read /
write proposals and responses against the queue store, append audit entries,
and render operator diffs. Only the orchestrator opens `bot-bottle.db` (PRD
0070), so this lives under `orchestrator/` — the data plane reaches it over the
control-plane RPC, never by importing this module.
"""

from __future__ import annotations

import difflib
import hashlib
import time
from pathlib import Path

from ...supervisor.types import (
    AuditEntry,
    DEFAULT_POLL_INTERVAL_SEC,
    Proposal,
    Response,
)
from ...paths import bot_bottle_root
from ..store.queue_store import QueueStore
from ...store.audit_store import AuditStore


# --- Paths -----------------------------------------------------------------


def audit_dir() -> Path:
    return bot_bottle_root() / "audit"


def audit_log_path(component: str, slug: str) -> Path:
    return audit_dir() / f"{component}-{slug}.log"


# --- Queue I/O -------------------------------------------------------------


def write_proposal(proposal: Proposal) -> Path:
    """Persist `proposal` in the queue database, mode 0o600.
    Directory is created if missing."""
    return QueueStore(proposal.bottle_slug).write_proposal(proposal)


def read_proposal(bottle_slug: str, proposal_id: str) -> Proposal:
    return QueueStore(bottle_slug).read_proposal(proposal_id)


def list_pending_proposals(bottle_slug: str) -> list[Proposal]:
    """All proposals for `bottle_slug` that do not yet have a matching
    response. Sorted by `arrival_timestamp` so the operator sees the queue
    FIFO."""
    return QueueStore(bottle_slug).list_pending_proposals()


def list_all_pending_proposals() -> list[Proposal]:
    """All pending proposals across bottles, sorted FIFO."""
    return QueueStore("").list_all_pending_proposals()


def write_response(bottle_slug: str, response: Response) -> Path:
    return QueueStore(bottle_slug).write_response(response)


def read_response(bottle_slug: str, proposal_id: str) -> Response:
    return QueueStore(bottle_slug).read_response(proposal_id)


def wait_for_response(
    bottle_slug: str,
    proposal_id: str,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
    deadline: float | None = None,
) -> Response:
    """Block until a response file appears for `proposal_id`, then return it.
    `deadline` is an absolute time.monotonic() value after which the wait raises
    TimeoutError. None waits forever — the natural shape, since the operator's
    response time is unbounded.

    Polls SQLite so the implementation stays portable and stdlib-only."""
    store = QueueStore(bottle_slug)
    while True:
        try:
            return store.read_response(proposal_id)
        except FileNotFoundError:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"no response for proposal {proposal_id!r}")
        time.sleep(poll_interval)


def archive_proposal(bottle_slug: str, proposal_id: str) -> None:
    """Mark both proposal and response rows processed.
    Idempotent — missing rows are silently skipped."""
    QueueStore(bottle_slug).archive_proposal(proposal_id)


def archive_all_proposals(bottle_slug: str) -> None:
    """Archive every proposal + response for `bottle_slug` (bottle teardown /
    reconcile cleanup). Idempotent."""
    QueueStore(bottle_slug).archive_all()


# --- Audit log -------------------------------------------------------------


def write_audit_entry(entry: AuditEntry) -> Path:
    """Append `entry` to the host supervise audit table."""
    return AuditStore().write_audit_entry(entry)


def read_audit_entries(component: str, slug: str) -> list[AuditEntry]:
    """Load all audit entries for the given component+slug."""
    return AuditStore().read_audit_entries(component, slug)


# --- Diff rendering --------------------------------------------------------


def render_diff(before: str, after: str, *, label: str = "config") -> str:
    """Unified diff suitable for the audit log + TUI. Empty diff (no changes)
    renders as the empty string."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} (current)",
        tofile=f"{label} (proposed)",
        lineterm="",
    )
    parts = list(diff)
    if not parts:
        return ""
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts).rstrip("\n")


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
