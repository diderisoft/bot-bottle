"""Shared types for supervise stores.

Extracted from supervise.py so queue_store and audit_store can import
Proposal, Response, and AuditEntry without creating a circular import
(supervise imports from queue_store/audit_store and vice-versa).

The path helpers (`bot_bottle_root`, `host_db_path`, `HOST_DB_FILENAME`)
live in `paths` — import them from there.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


TOOL_EGRESS_BLOCK = "egress-block"
TOOL_EGRESS_ALLOW = "egress-allow"
TOOL_GITLEAKS_ALLOW = "gitleaks-allow"
TOOL_EGRESS_TOKEN_ALLOW = "egress-token-allow"
TOOL_LIST_EGRESS_ROUTES = "list-egress-routes"
# Read-only agent tool: poll a queued proposal for the operator's decision
# without blocking or re-proposing. It never becomes a `Proposal.tool` (no
# queue record is created for it), so it is intentionally NOT in `TOOLS`.
TOOL_CHECK_PROPOSAL = "check-proposal"
TOOLS: tuple[str, ...] = (
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
    TOOL_LIST_EGRESS_ROUTES,
)

STATUS_APPROVED = "approved"
STATUS_MODIFIED = "modified"
STATUS_REJECTED = "rejected"
STATUSES: tuple[str, ...] = (STATUS_APPROVED, STATUS_MODIFIED, STATUS_REJECTED)

# Non-terminal markers the control-plane supervise-poll RPC returns when a
# proposal has no operator decision yet (`PENDING`) or no queued proposal with
# that id exists for the calling bottle (`UNKNOWN`). They are never a
# `Response.status` — only the poll wire contract (see
# `OrchestratorCore.supervise_poll_response`) — so they are deliberately outside
# `STATUSES`.
POLL_STATUS_PENDING = "pending"
POLL_STATUS_UNKNOWN = "unknown"

ACTION_OPERATOR_EDIT = "operator-edit"

# Shared supervise-daemon constants. The gateway `supervise_server` and the
# backend read these, so they live with the wire types in the neutral package
# (never in the orchestrator's private code the data plane can't import).
SUPERVISE_HOSTNAME = "supervise"
SUPERVISE_PORT = 9100
# The supervise daemon queries egress's introspection endpoint for the
# `list-egress-routes` tool over loopback (egress runs alongside it in the
# gateway), stable across docker / firecracker / Apple Container backends.
EGRESS_FORWARD_PROXY = "http://127.0.0.1:9099"
EGRESS_INTROSPECT_URL = "http://_egress.local/allowlist"
DB_PATH_IN_CONTAINER = "/run/supervise/bot-bottle.db"
DEFAULT_POLL_INTERVAL_SEC = 0.5

COMPONENT_FOR_TOOL: dict[str, str] = {
    TOOL_EGRESS_ALLOW: "egress",
    TOOL_EGRESS_BLOCK: "egress",
}


def _require_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"missing or non-string field {key!r}")
    return value


@dataclass(frozen=True)
class Proposal:
    """One pending tool-call from the agent."""

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
            current_file_hash=hashlib.sha256(proposed_file.encode("utf-8")).hexdigest(),
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
    """The operator's decision on a proposal."""

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
    """One row of the per-bottle audit log."""

    timestamp: str
    bottle_slug: str
    component: str
    operator_action: str
    operator_notes: str
    justification: str
    diff: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


__all__ = [
    "ACTION_OPERATOR_EDIT",
    "AuditEntry",
    "Proposal",
    "Response",
    "STATUSES",
    "STATUS_APPROVED",
    "STATUS_MODIFIED",
    "STATUS_REJECTED",
    "POLL_STATUS_PENDING",
    "POLL_STATUS_UNKNOWN",
    "TOOLS",
    "TOOL_EGRESS_ALLOW",
    "TOOL_EGRESS_BLOCK",
    "TOOL_CHECK_PROPOSAL",
    "TOOL_EGRESS_TOKEN_ALLOW",
    "TOOL_GITLEAKS_ALLOW",
    "TOOL_LIST_EGRESS_ROUTES",
    "SUPERVISE_HOSTNAME",
    "SUPERVISE_PORT",
    "EGRESS_FORWARD_PROXY",
    "EGRESS_INTROSPECT_URL",
    "DB_PATH_IN_CONTAINER",
    "DEFAULT_POLL_INTERVAL_SEC",
    "COMPONENT_FOR_TOOL",
]
