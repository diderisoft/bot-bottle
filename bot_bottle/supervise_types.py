"""Shared types and path helpers for supervise stores (PRD 0013).

Extracted from supervise.py so queue_store and audit_store can import
Proposal, Response, AuditEntry, and host_db_path without creating a
circular import (supervise imports from queue_store/audit_store and
vice-versa).

host_db_path / HOST_DB_FILENAME now live in db_store (shared DB infra) and
are re-exported here for the historical import path. db_store.host_db_path
resolves bot_bottle_root from this module lazily, so patching
supervise_types.bot_bottle_root still propagates to the DB path.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# host_db_path / HOST_DB_FILENAME now live in db_store (shared DB infra);
# re-exported here for the historical import path. db_store resolves
# bot_bottle_root from this module lazily, so patching
# supervise_types.bot_bottle_root still propagates.
try:
    from .db_store import HOST_DB_FILENAME, host_db_path
except ImportError:  # flat imports inside the sidecar bundle
    from db_store import HOST_DB_FILENAME, host_db_path  # type: ignore[import-not-found,no-redef]  # pylint: disable=import-error,no-name-in-module

TOOL_EGRESS_BLOCK = "egress-block"
TOOL_EGRESS_ALLOW = "egress-allow"
TOOL_GITLEAKS_ALLOW = "gitleaks-allow"
TOOL_EGRESS_TOKEN_ALLOW = "egress-token-allow"
TOOL_LIST_EGRESS_ROUTES = "list-egress-routes"
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

ACTION_OPERATOR_EDIT = "operator-edit"


def bot_bottle_root() -> Path:
    return Path.home() / ".bot-bottle"


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
    "HOST_DB_FILENAME",
    "Proposal",
    "Response",
    "STATUSES",
    "STATUS_APPROVED",
    "STATUS_MODIFIED",
    "STATUS_REJECTED",
    "TOOLS",
    "TOOL_EGRESS_ALLOW",
    "TOOL_EGRESS_BLOCK",
    "TOOL_EGRESS_TOKEN_ALLOW",
    "TOOL_GITLEAKS_ALLOW",
    "TOOL_LIST_EGRESS_ROUTES",
    "bot_bottle_root",
    "host_db_path",
]
