"""Supervise wire types and constants: `Proposal`, `Response`, `AuditEntry`."""

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
# Ask the operator to connect a forge (PRD 0105); `proposed_file` is its URL.
# The operator supplies the token on the phone; it never reaches the agent.
TOOL_FORGE_CONNECT = "forge-connect"
# Read-only agent tool that polls a proposal's decision. It never becomes a
# `Proposal.tool`, so it isn't in `TOOLS`.
TOOL_CHECK_PROPOSAL = "check-proposal"
TOOLS: tuple[str, ...] = (
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
    TOOL_LIST_EGRESS_ROUTES,
    TOOL_FORGE_CONNECT,
)

STATUS_APPROVED = "approved"
STATUS_MODIFIED = "modified"
STATUS_REJECTED = "rejected"
STATUSES: tuple[str, ...] = (STATUS_APPROVED, STATUS_MODIFIED, STATUS_REJECTED)

# Poll-only statuses for a proposal with no decision yet, or no such proposal
# for the caller. Never a `Response.status`.
POLL_STATUS_PENDING = "pending"
POLL_STATUS_UNKNOWN = "unknown"

# The start of `/supervise/respond`'s 409 detail for a proposal that already
# has a decision; the first answer stands.
ALREADY_ANSWERED = "already answered"

ACTION_OPERATOR_EDIT = "operator-edit"

# Supervise constants shared by the gateway and the backends.
SUPERVISE_PORT = 9100
# supervise queries egress's introspection endpoint over loopback for
# `list-egress-routes`.
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
    """One pending tool call from the agent."""

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
    "ALREADY_ANSWERED",
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
    "SUPERVISE_PORT",
    "EGRESS_FORWARD_PROXY",
    "EGRESS_INTROSPECT_URL",
    "DB_PATH_IN_CONTAINER",
    "DEFAULT_POLL_INTERVAL_SEC",
    "COMPONENT_FOR_TOOL",
]
