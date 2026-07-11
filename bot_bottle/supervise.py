"""Per-bottle supervise plane (PRD 0013).

The supervise plane is the per-bottle MCP sidecar plus its host-side
queue/audit support. The sidecar (bot_bottle.supervise_server)
sits on the bottle's internal network and exposes MCP tools the agent
calls when it needs an operator-reviewed egress change:

  * egress-block / allow — agent proposes a new routes.yaml

Each tool call: the agent passes the full proposed file plus a
justification text. The sidecar validates the proposal syntactically,
writes it to the host SQLite queue table, and holds the tool-call
connection open. The operator's supervise TUI
(bot_bottle.cli.supervise) sees the proposal, accepts
approve / modify / reject, and writes a response row. The sidecar sees
the response and returns `{status, notes}` to the agent.

This module defines the host-side library: dataclasses for the queue
record shapes, queue read/write helpers, the audit log writer, and the
diff renderer. The in-container sidecar lives in
bot_bottle/supervise_server.py; the supervise daemon's container
lifecycle is owned by the sidecar bundle (PRD 0024).

For 0013 the supervisor's approval handlers are deliberately no-ops:
on approval the audit log is written and the response file is
delivered to the agent, but no host-side config change happens. The
remediation engines that wire real config changes land in PRDs 0014,
0015, and 0016.
"""

from __future__ import annotations

import difflib
import hashlib
import time
from abc import ABC
from dataclasses import dataclass
from pathlib import Path

try:
    from .supervise_types import (
        ACTION_OPERATOR_EDIT,
        AuditEntry,
        HOST_DB_FILENAME,
        Proposal,
        Response,
        STATUSES,
        STATUS_APPROVED,
        STATUS_MODIFIED,
        STATUS_REJECTED,
        TOOLS,
        TOOL_EGRESS_ALLOW,
        TOOL_EGRESS_BLOCK,
        TOOL_EGRESS_TOKEN_ALLOW,
        TOOL_GITLEAKS_ALLOW,
        TOOL_LIST_EGRESS_ROUTES,
        bot_bottle_root,
    )
except ImportError:
    from supervise_types import (  # type: ignore[import-not-found,no-redef]  # pylint: disable=import-error,no-name-in-module
        ACTION_OPERATOR_EDIT,
        AuditEntry,
        HOST_DB_FILENAME,
        Proposal,
        Response,
        STATUSES,
        STATUS_APPROVED,
        STATUS_MODIFIED,
        STATUS_REJECTED,
        TOOLS,
        TOOL_EGRESS_ALLOW,
        TOOL_EGRESS_BLOCK,
        TOOL_EGRESS_TOKEN_ALLOW,
        TOOL_GITLEAKS_ALLOW,
        TOOL_LIST_EGRESS_ROUTES,
        bot_bottle_root,
    )


SUPERVISE_HOSTNAME = "supervise"
SUPERVISE_PORT = 9100

# The supervise sidecar uses these to query egress's
# introspection endpoint for the `list-egress-routes` MCP
# tool. The hostname + port match egress's docker network
# listen port (see backend.docker.egress.EGRESS_PORT). The supervise
# daemon runs inside the sidecar bundle alongside egress, so loopback
# is the stable address across docker, firecracker, and Apple
# Container backends.
EGRESS_FORWARD_PROXY = "http://127.0.0.1:9099"
EGRESS_INTROSPECT_URL = "http://_egress.local/allowlist"

COMPONENT_FOR_TOOL: dict[str, str] = {
    TOOL_EGRESS_ALLOW: "egress",
    TOOL_EGRESS_BLOCK: "egress",
}

DB_PATH_IN_CONTAINER = "/run/supervise/bot-bottle.db"
DEFAULT_POLL_INTERVAL_SEC = 0.5


# --- Paths -----------------------------------------------------------------


def audit_dir() -> Path:
    return bot_bottle_root() / "audit"


def host_db_path() -> Path:
    # Calls bot_bottle_root() through this module's globals so that patches
    # on supervise.bot_bottle_root propagate to callers going through
    # supervise.host_db_path().
    #
    # Kept in its own "db" subdirectory (see supervise_types.host_db_path
    # for why) — must stay in sync with that copy.
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


def audit_log_path(component: str, slug: str) -> Path:
    return audit_dir() / f"{component}-{slug}.log"


try:
    from .queue_store import QueueStore
    from .audit_store import AuditStore
    from .store_manager import StoreManager
except ImportError:
    # Sidecar bundle: files are flat-copied under /app, not a package.
    from queue_store import QueueStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from audit_store import AuditStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from store_manager import StoreManager  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


# --- Queue I/O -------------------------------------------------------------


def write_proposal(proposal: Proposal) -> Path:
    """Persist `proposal` in the queue database, mode 0o600.
    Directory is created if missing."""
    return QueueStore(proposal.bottle_slug).write_proposal(proposal)


def read_proposal(bottle_slug: str, proposal_id: str) -> Proposal:
    return QueueStore(bottle_slug).read_proposal(proposal_id)


def list_pending_proposals(bottle_slug: str) -> list[Proposal]:
    """All proposals for `bottle_slug` that do not yet have a matching
    response. Sorted by `arrival_timestamp` so the operator
    sees the queue FIFO."""
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
    """Block until a response file appears for `proposal_id`, then
    return it. `deadline` is an absolute time.monotonic() value after
    which the wait raises TimeoutError. None waits forever — the
    natural shape, since the operator's response time is unbounded.

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


# --- Audit log -------------------------------------------------------------


def write_audit_entry(entry: AuditEntry) -> Path:
    """Append `entry` to the host supervise audit table."""
    return AuditStore().write_audit_entry(entry)


def read_audit_entries(component: str, slug: str) -> list[AuditEntry]:
    """Load all audit entries for the given component+slug."""
    return AuditStore().read_audit_entries(component, slug)


# --- Diff rendering --------------------------------------------------------


def render_diff(before: str, after: str, *, label: str = "config") -> str:
    """Unified diff suitable for the audit log + TUI. Empty diff (no
    changes) renders as the empty string."""
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


# --- Sidecar plan + abstract lifecycle -------------------------------------


@dataclass(frozen=True)
class SupervisePlan:
    """Output of Supervise.prepare; consumed by .start.

    `db_path` is the host database bind-mounted into the sidecar at
    /run/supervise/bot-bottle.db. `internal_network` is empty at
    prepare time; the backend's launch step fills it via
    dataclasses.replace before calling .start."""

    slug: str
    db_path: Path
    internal_network: str = ""


class Supervise(ABC):
    """Per-bottle supervise sidecar. Encapsulates host-side database
    staging; the sidecar's start/stop lifecycle is backend-specific."""

    def prepare(
        self,
        slug: str,
        stage_dir: Path,
    ) -> SupervisePlan:
        """Stage the host database. Returns the plan; `internal_network`
        must be set by the launch step before .start runs."""
        del stage_dir
        mgr = StoreManager.instance()
        mgr.migrate()
        return SupervisePlan(
            slug=slug,
            db_path=mgr.db_path,
        )


__all__ = [
    "ACTION_OPERATOR_EDIT",
    "AuditEntry",
    "AuditStore",
    "COMPONENT_FOR_TOOL",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DB_PATH_IN_CONTAINER",
    "Proposal",
    "QueueStore",
    "Response",
    "StoreManager",
    "STATUSES",
    "STATUS_APPROVED",
    "STATUS_MODIFIED",
    "STATUS_REJECTED",
    "SUPERVISE_HOSTNAME",
    "SUPERVISE_PORT",
    "Supervise",
    "SupervisePlan",
    "TOOLS",
    "EGRESS_FORWARD_PROXY",
    "EGRESS_INTROSPECT_URL",
    "TOOL_EGRESS_ALLOW",
    "TOOL_EGRESS_BLOCK",
    "TOOL_GITLEAKS_ALLOW",
    "TOOL_EGRESS_TOKEN_ALLOW",
    "TOOL_LIST_EGRESS_ROUTES",
    "archive_proposal",
    "audit_dir",
    "audit_log_path",
    "bot_bottle_root",
    "host_db_path",
    "list_pending_proposals",
    "list_all_pending_proposals",
    "read_audit_entries",
    "read_proposal",
    "read_response",
    "render_diff",
    "sha256_hex",
    "wait_for_response",
    "write_audit_entry",
    "write_proposal",
    "write_response",
]
