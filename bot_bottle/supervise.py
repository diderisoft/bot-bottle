"""Per-bottle supervise plane (PRD 0013).

The supervise plane is the per-bottle MCP sidecar plus its host-side
queue/audit support. The sidecar (bot_bottle.supervise_server)
sits on the bottle's internal network and exposes MCP tools the agent
calls when it needs an operator-reviewed egress change:

  * egress-block / allow — agent proposes a new routes.yaml

Each tool call: the agent passes the full proposed file plus a
justification text. The sidecar validates the proposal syntactically,
writes it to the host's per-bottle queue dir, and holds the tool-call
connection open. The operator's supervise TUI
(bot_bottle.cli.supervise) sees the proposal, accepts
approve / modify / reject, and writes a response file alongside the
proposal. The sidecar sees the response and returns `{status, notes}`
to the agent.

This module defines the host-side library: dataclasses for the queue
file shapes, queue read/write helpers, the audit log writer, and the
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

import dataclasses
import difflib
import hashlib
import sqlite3
import time
import uuid
from abc import ABC
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SUPERVISE_HOSTNAME = "supervise"
SUPERVISE_PORT = 9100

TOOL_EGRESS_BLOCK = "egress-block"
TOOL_EGRESS_ALLOW = "egress-allow"
TOOL_GITLEAKS_ALLOW = "gitleaks-allow"
# Written directly by the egress addon (not an agent-facing MCP tool) when an
# outbound DLP token block is routed to the operator for override (PRD 0062).
TOOL_EGRESS_TOKEN_ALLOW = "egress-token-allow"
TOOL_LIST_EGRESS_ROUTES = "list-egress-routes"
TOOLS: tuple[str, ...] = (
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
    TOOL_LIST_EGRESS_ROUTES,
)

# The supervise sidecar uses these to query egress's
# introspection endpoint for the `list-egress-routes` MCP
# tool. The hostname + port match egress's docker network
# listen port (see backend.docker.egress.EGRESS_PORT). The supervise
# daemon runs inside the sidecar bundle alongside egress, so loopback
# is the stable address across docker, smolmachines, and Apple
# Container backends.
EGRESS_FORWARD_PROXY = "http://127.0.0.1:9099"
EGRESS_INTROSPECT_URL = "http://_egress.local/allowlist"

COMPONENT_FOR_TOOL: dict[str, str] = {
    TOOL_EGRESS_ALLOW: "egress",
    TOOL_EGRESS_BLOCK: "egress",
}

STATUS_APPROVED = "approved"
STATUS_MODIFIED = "modified"
STATUS_REJECTED = "rejected"
STATUSES: tuple[str, ...] = (STATUS_APPROVED, STATUS_MODIFIED, STATUS_REJECTED)

# Operator-initiated audit entries (no tool call). PRD 0014's
# `routes edit <bottle>` verb writes entries with this action.
ACTION_OPERATOR_EDIT = "operator-edit"

QUEUE_DIR_IN_CONTAINER = "/run/supervise/queue"
DEFAULT_POLL_INTERVAL_SEC = 0.5
HOST_DB_FILENAME = "bot-bottle.db"
QUEUE_DB_FILENAME = "supervise.db"


# --- Paths -----------------------------------------------------------------


def bot_bottle_root() -> Path:
    return Path.home() / ".bot-bottle"


def queue_dir_for_slug(slug: str) -> Path:
    return bot_bottle_root() / "queue" / slug


def audit_dir() -> Path:
    return bot_bottle_root() / "audit"


def audit_log_path(component: str, slug: str) -> Path:
    return audit_dir() / f"{component}-{slug}.log"


def host_db_path() -> Path:
    return bot_bottle_root() / HOST_DB_FILENAME


def queue_db_path(queue_dir: Path) -> Path:
    return queue_dir / QUEUE_DB_FILENAME


# --- Dataclasses -----------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """One pending tool-call from the agent. The sidecar writes one
    of these to the queue dir on a tool call; the operator's TUI
    reads them; the sidecar polls for a matching Response."""

    id: str
    bottle_slug: str
    tool: str
    proposed_file: str
    justification: str
    arrival_timestamp: str
    current_file_hash: str

    @classmethod
    def new(
        cls,
        *,
        bottle_slug: str,
        tool: str,
        proposed_file: str,
        justification: str,
        current_file_hash: str,
        now: datetime | None = None,
    ) -> "Proposal":
        ts = (now or datetime.now(timezone.utc)).isoformat()
        return cls(
            id=str(uuid.uuid4()),
            bottle_slug=bottle_slug,
            tool=tool,
            proposed_file=proposed_file,
            justification=justification,
            arrival_timestamp=ts,
            current_file_hash=current_file_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Proposal":
        tool = _require_str(raw, "tool")
        if tool not in TOOLS:
            raise ValueError(f"tool must be one of {TOOLS}; got {tool!r}")
        return cls(
            id=_require_str(raw, "id"),
            bottle_slug=_require_str(raw, "bottle_slug"),
            tool=tool,
            proposed_file=_require_str(raw, "proposed_file"),
            justification=_require_str(raw, "justification"),
            arrival_timestamp=_require_str(raw, "arrival_timestamp"),
            current_file_hash=_require_str(raw, "current_file_hash"),
        )


@dataclass(frozen=True)
class Response:
    """The operator's decision on a proposal. The TUI writes one of
    these to the queue dir; the sidecar reads it and returns the
    `{status, notes}` pair to the agent's tool call.

    `final_file` carries the file content the supervisor will
    actually apply: for `approved`, equal to the proposal's
    `proposed_file`; for `modified`, the operator's edited version
    (the audit diff is current → final_file, not current →
    proposed_file); for `rejected`, None."""

    proposal_id: str
    status: str
    notes: str
    final_file: str | None = None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Response":
        status = _require_str(raw, "status")
        if status not in STATUSES:
            raise ValueError(
                f"response status must be one of {STATUSES}; got {status!r}"
            )
        final = raw.get("final_file")
        if final is not None and not isinstance(final, str):
            raise ValueError(
                f"final_file must be a string or null; got {type(final).__name__}"
            )
        return cls(
            proposal_id=_require_str(raw, "proposal_id"),
            status=status,
            notes=_require_str(raw, "notes"),
            final_file=final,
        )


@dataclass(frozen=True)
class AuditEntry:
    """One row of the per-bottle audit log. JSON-Lines, append-only."""

    timestamp: str
    bottle_slug: str
    component: str
    operator_action: str
    operator_notes: str
    justification: str
    diff: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


# --- Queue I/O -------------------------------------------------------------


def write_proposal(queue_dir: Path, proposal: Proposal) -> Path:
    """Persist `proposal` in the queue database, mode 0o600.
    Directory is created if missing."""
    return _QueueStore(queue_dir).write_proposal(proposal)


def read_proposal(queue_dir: Path, proposal_id: str) -> Proposal:
    return _QueueStore(queue_dir).read_proposal(proposal_id)


def list_pending_proposals(queue_dir: Path) -> list[Proposal]:
    """All proposals in `queue_dir` that do not yet have a matching
    response. Sorted by `arrival_timestamp` so the operator
    sees the queue FIFO."""
    return _QueueStore(queue_dir).list_pending_proposals()


def write_response(queue_dir: Path, response: Response) -> Path:
    return _QueueStore(queue_dir).write_response(response)


def read_response(queue_dir: Path, proposal_id: str) -> Response:
    return _QueueStore(queue_dir).read_response(proposal_id)


def wait_for_response(
    queue_dir: Path,
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
    store = _QueueStore(queue_dir)
    while True:
        try:
            return store.read_response(proposal_id)
        except FileNotFoundError:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"no response for proposal {proposal_id!r}")
        time.sleep(poll_interval)


def archive_proposal(queue_dir: Path, proposal_id: str) -> None:
    """Mark both proposal and response rows processed.
    Idempotent — missing rows are silently skipped."""
    _QueueStore(queue_dir).archive_proposal(proposal_id)


# --- Audit log -------------------------------------------------------------


def write_audit_entry(entry: AuditEntry) -> Path:
    """Append `entry` to the host supervise audit table."""
    return _AuditStore().write_audit_entry(entry)


def read_audit_entries(component: str, slug: str) -> list[AuditEntry]:
    """Load all audit entries for the given component+slug."""
    return _AuditStore().read_audit_entries(component, slug)


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


# --- SQLite storage --------------------------------------------------------


class _QueueStore:
    def __init__(self, queue_dir: Path) -> None:
        self.db_path = queue_db_path(queue_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def write_proposal(self, proposal: Proposal) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_proposals (
                    id, bottle_slug, tool, proposed_file, justification,
                    arrival_timestamp, current_file_hash, archived
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    proposal.id,
                    proposal.bottle_slug,
                    proposal.tool,
                    proposal.proposed_file,
                    proposal.justification,
                    proposal.arrival_timestamp,
                    proposal.current_file_hash,
                ),
            )
        self._chmod()
        return self.db_path

    def read_proposal(self, proposal_id: str) -> Proposal:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_proposals
                WHERE id = ? AND archived = 0
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return _proposal_from_row(row)

    def list_pending_proposals(self) -> list[Proposal]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM supervise_proposals p
                WHERE p.archived = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM supervise_responses r
                    WHERE r.proposal_id = p.id AND r.archived = 0
                  )
                ORDER BY p.arrival_timestamp, p.id
                """
            ).fetchall()
        return [_proposal_from_row(row) for row in rows]

    def write_response(self, response: Response) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_responses (
                    proposal_id, status, notes, final_file, archived
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (
                    response.proposal_id,
                    response.status,
                    response.notes,
                    response.final_file,
                ),
            )
        self._chmod()
        return self.db_path

    def read_response(self, proposal_id: str) -> Response:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_responses
                WHERE proposal_id = ? AND archived = 0
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return _response_from_row(row)

    def archive_proposal(self, proposal_id: str) -> None:
        if not self.db_path.is_file():
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE supervise_proposals SET archived = 1 WHERE id = ?",
                (proposal_id,),
            )
            conn.execute(
                """
                UPDATE supervise_responses SET archived = 1
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supervise_proposals (
                    id TEXT PRIMARY KEY,
                    bottle_slug TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    proposed_file TEXT NOT NULL,
                    justification TEXT NOT NULL,
                    arrival_timestamp TEXT NOT NULL,
                    current_file_hash TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supervise_responses (
                    proposal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    final_file TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


class _AuditStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or host_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def write_audit_entry(self, entry: AuditEntry) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO supervise_audit_entries (
                    timestamp, bottle_slug, component, operator_action,
                    operator_notes, justification, diff
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.timestamp,
                    entry.bottle_slug,
                    entry.component,
                    entry.operator_action,
                    entry.operator_notes,
                    entry.justification,
                    entry.diff,
                ),
            )
        self._chmod()
        return self.db_path

    def read_audit_entries(self, component: str, slug: str) -> list[AuditEntry]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM supervise_audit_entries
                WHERE component = ? AND bottle_slug = ?
                ORDER BY id
                """,
                (component, slug),
            ).fetchall()
        return [_audit_entry_from_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS supervise_audit_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    bottle_slug TEXT NOT NULL,
                    component TEXT NOT NULL,
                    operator_action TEXT NOT NULL,
                    operator_notes TEXT NOT NULL,
                    justification TEXT NOT NULL,
                    diff TEXT NOT NULL
                )
                """
            )
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


def _proposal_from_row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        bottle_slug=row["bottle_slug"],
        tool=row["tool"],
        proposed_file=row["proposed_file"],
        justification=row["justification"],
        arrival_timestamp=row["arrival_timestamp"],
        current_file_hash=row["current_file_hash"],
    )


def _response_from_row(row: sqlite3.Row) -> Response:
    return Response(
        proposal_id=row["proposal_id"],
        status=row["status"],
        notes=row["notes"],
        final_file=row["final_file"],
    )


def _audit_entry_from_row(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        timestamp=row["timestamp"],
        bottle_slug=row["bottle_slug"],
        component=row["component"],
        operator_action=row["operator_action"],
        operator_notes=row["operator_notes"],
        justification=row["justification"],
        diff=row["diff"],
    )


# --- Sidecar plan + abstract lifecycle -------------------------------------


@dataclass(frozen=True)
class SupervisePlan:
    """Output of Supervise.prepare; consumed by .start.

    `queue_dir` is the host directory bind-mounted into the sidecar
    at /run/supervise/queue. `internal_network` is empty at prepare
    time; the backend's launch step fills it via dataclasses.replace
    before calling .start."""

    slug: str
    queue_dir: Path
    internal_network: str = ""


class Supervise(ABC):
    """Per-bottle supervise sidecar. Encapsulates the host-side
    prepare (queue dir staging); the sidecar's start/stop lifecycle
    is backend-specific."""

    def prepare(
        self,
        slug: str,
        stage_dir: Path,
    ) -> SupervisePlan:
        """Stage the per-bottle queue dir on the host. Returns the
        plan; `internal_network` must be set by the launch step before
        .start runs."""
        del stage_dir
        queue_dir = queue_dir_for_slug(slug)
        queue_dir.mkdir(parents=True, exist_ok=True)
        return SupervisePlan(
            slug=slug,
            queue_dir=queue_dir,
        )

# --- Helpers ---------------------------------------------------------------


def _require_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"missing or non-string field {key!r}")
    return value


__all__ = [
    "ACTION_OPERATOR_EDIT",
    "AuditEntry",
    "COMPONENT_FOR_TOOL",
    "DEFAULT_POLL_INTERVAL_SEC",
    "Proposal",
    "QUEUE_DIR_IN_CONTAINER",
    "Response",
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
    "queue_db_path",
    "queue_dir_for_slug",
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
