"""Per-bottle supervise plane (PRD 0013).

The supervise plane is the per-bottle MCP sidecar plus its host-side
queue/audit support. The sidecar (bot_bottle.supervise_server)
sits on the bottle's internal network and exposes three MCP tools the
agent calls when it hits a stuck-recovery category:

  * egress-block / allow — agent proposes a new routes.yaml
  * capability-block — agent proposes a new agent Dockerfile

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
import json
import os
import time
import uuid
from abc import ABC
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SUPERVISE_HOSTNAME = "supervise"
SUPERVISE_PORT = 9100

TOOL_CAPABILITY_BLOCK = "capability-block"
TOOL_EGRESS_BLOCK = "egress-block"
TOOL_ALLOW = "allow"
TOOL_GITLEAKS_ALLOW = "gitleaks-allow"
TOOL_LIST_EGRESS_ROUTES = "list-egress-routes"
TOOLS: tuple[str, ...] = (
    TOOL_ALLOW,
    TOOL_CAPABILITY_BLOCK,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
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

# capability-block has no on-disk config the operator edits in place
# (the Dockerfile is rebuilt, not patched), so it has no audit log
# here — those changes are captured by git history + the rebuild record
# laid down in PRD 0016.
COMPONENT_FOR_TOOL: dict[str, str] = {
    TOOL_ALLOW: "egress",
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
CURRENT_CONFIG_DIR_IN_AGENT = "/etc/bot-bottle/current-config"

DEFAULT_POLL_INTERVAL_SEC = 0.5


# --- Paths -----------------------------------------------------------------


def bot_bottle_root() -> Path:
    return Path.home() / ".bot-bottle"


def queue_dir_for_slug(slug: str) -> Path:
    return bot_bottle_root() / "queue" / slug


def audit_dir() -> Path:
    return bot_bottle_root() / "audit"


def audit_log_path(component: str, slug: str) -> Path:
    return audit_dir() / f"{component}-{slug}.log"


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


def _proposal_filename(proposal_id: str) -> str:
    return f"{proposal_id}.proposal.json"


def _response_filename(proposal_id: str) -> str:
    return f"{proposal_id}.response.json"


def _id_from_proposal_filename(path: Path) -> str | None:
    name = path.name
    if not name.endswith(".proposal.json"):
        return None
    return name[: -len(".proposal.json")]


def write_proposal(queue_dir: Path, proposal: Proposal) -> Path:
    """Persist `proposal` as JSON in the queue dir, mode 0o600.
    Directory is created if missing."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / _proposal_filename(proposal.id)
    payload = json.dumps(proposal.to_dict(), indent=2) + "\n"
    _atomic_write(path, payload, mode=0o600)
    return path


def read_proposal(queue_dir: Path, proposal_id: str) -> Proposal:
    path = queue_dir / _proposal_filename(proposal_id)
    with path.open() as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be an object")
    return Proposal.from_dict(raw)


def list_pending_proposals(queue_dir: Path) -> list[Proposal]:
    """All proposals in `queue_dir` that do not yet have a matching
    response file. Sorted by `arrival_timestamp` so the operator
    sees the queue FIFO."""
    if not queue_dir.is_dir():
        return []
    out: list[Proposal] = []
    for path in sorted(queue_dir.glob("*.proposal.json")):
        proposal_id = _id_from_proposal_filename(path)
        if proposal_id is None:
            continue
        if (queue_dir / _response_filename(proposal_id)).exists():
            continue
        try:
            with path.open() as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            out.append(Proposal.from_dict(raw))
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda p: p.arrival_timestamp)
    return out


def write_response(queue_dir: Path, response: Response) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / _response_filename(response.proposal_id)
    payload = json.dumps(response.to_dict(), indent=2) + "\n"
    _atomic_write(path, payload, mode=0o600)
    return path


def read_response(queue_dir: Path, proposal_id: str) -> Response:
    path = queue_dir / _response_filename(proposal_id)
    with path.open() as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be an object")
    return Response.from_dict(raw)


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

    Polls the filesystem so the implementation stays portable and
    stdlib-only."""
    path = queue_dir / _response_filename(proposal_id)
    while True:
        if path.exists():
            try:
                with path.open() as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                try:
                    return Response.from_dict(raw)
                except (KeyError, ValueError):
                    pass
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"no response for proposal {proposal_id!r}")
        time.sleep(poll_interval)


def archive_proposal(queue_dir: Path, proposal_id: str) -> None:
    """Move both proposal and response files to `<queue_dir>/processed/`.
    Idempotent — missing files are silently skipped."""
    processed = queue_dir / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    for name in (_proposal_filename(proposal_id), _response_filename(proposal_id)):
        src = queue_dir / name
        if src.exists():
            src.rename(processed / name)


# --- Audit log -------------------------------------------------------------


def write_audit_entry(entry: AuditEntry) -> Path:
    """Append `entry` as one JSON-Lines record to the per-bottle
    audit log. Acquires an advisory exclusive lock so concurrent
    writers don't interleave bytes."""
    path = audit_log_path(entry.component, entry.bottle_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.to_dict(), sort_keys=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _try_flock(fd)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            _try_funlock(fd)
    finally:
        os.close(fd)
    return path


def read_audit_entries(component: str, slug: str) -> list[AuditEntry]:
    """Load all audit entries for the given component+slug. Empty
    list if the log doesn't exist."""
    path = audit_log_path(component, slug)
    if not path.is_file():
        return []
    out: list[AuditEntry] = []
    with path.open() as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                out.append(AuditEntry(
                    timestamp=_require_str(raw, "timestamp"),
                    bottle_slug=_require_str(raw, "bottle_slug"),
                    component=_require_str(raw, "component"),
                    operator_action=_require_str(raw, "operator_action"),
                    operator_notes=_require_str(raw, "operator_notes"),
                    justification=_require_str(raw, "justification"),
                    diff=_require_str(raw, "diff"),
                ))
            except ValueError:
                continue
    return out


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


# Filename of the staged Dockerfile inside the agent's read-only
# current-config mount. The capability-block tool's description
# points the agent at this exact path so it can read the current
# Dockerfile and propose modifications.
#
# routes.yaml + allowlist used to live here too; PRD 0017 chunk 3
# moved them behind the `list-egress-routes` MCP tool (live state
# from egress's introspection endpoint) so the agent always sees
# current data rather than a launch-time snapshot.
CURRENT_CONFIG_DOCKERFILE = "Dockerfile"


@dataclass(frozen=True)
class SupervisePlan:
    """Output of Supervise.prepare; consumed by .start.

    `queue_dir` is the host directory bind-mounted into the sidecar
    at /run/supervise/queue. `current_config_dir` is the host
    directory bind-mounted (read-only) into the *agent* container
    at /etc/bot-bottle/current-config — currently holds only the
    Dockerfile snapshot (routes.yaml + allowlist moved to the
    `list-egress-routes` MCP tool). `internal_network` is
    empty at prepare time; the backend's launch step fills it via
    dataclasses.replace before calling .start."""

    slug: str
    queue_dir: Path
    current_config_dir: Path
    internal_network: str = ""


class Supervise(ABC):
    """Per-bottle supervise sidecar. Encapsulates the host-side
    prepare (queue dir + current-config staging); the sidecar's
    start/stop lifecycle is backend-specific."""

    def prepare(
        self,
        slug: str,
        stage_dir: Path,
    ) -> SupervisePlan:
        """Stage the per-bottle queue dir on the host and the
        current-config dir under `stage_dir`. Returns the plan;
        `internal_network` must be set by the launch step before
        .start runs."""
        queue_dir = queue_dir_for_slug(slug)
        queue_dir.mkdir(parents=True, exist_ok=True)
        current_config_dir = stage_dir / "current-config"
        current_config_dir.mkdir(parents=True, exist_ok=True)
        return SupervisePlan(
            slug=slug,
            queue_dir=queue_dir,
            current_config_dir=current_config_dir,
        )

# --- Helpers ---------------------------------------------------------------


def _require_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"missing or non-string field {key!r}")
    return value


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    """Atomic: write to a sibling tmp file, fsync, rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


try:
    import fcntl as _fcntl

    def _try_flock(fd: int) -> None:  # type: ignore[reportRedeclaration]
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        except OSError:
            pass

    def _try_funlock(fd: int) -> None:  # type: ignore[reportRedeclaration]
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
except ImportError:  # pragma: no cover — Windows path
    def _try_flock(fd: int) -> None:  # noqa: F841 — Windows fallback
        return None

    def _try_funlock(fd: int) -> None:  # noqa: F841 — Windows fallback
        return None


__all__ = [
    "ACTION_OPERATOR_EDIT",
    "AuditEntry",
    "COMPONENT_FOR_TOOL",
    "CURRENT_CONFIG_DIR_IN_AGENT",
    "CURRENT_CONFIG_DOCKERFILE",
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
    "TOOL_CAPABILITY_BLOCK",
    "TOOL_GITLEAKS_ALLOW",
    "TOOL_LIST_EGRESS_ROUTES",
    "archive_proposal",
    "audit_dir",
    "audit_log_path",
    "bot_bottle_root",
    "list_pending_proposals",
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
