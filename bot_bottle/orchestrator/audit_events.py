"""The orchestrator's audit events: who it let in and turned away, the
identity tokens it minted, and supervise proposals and their decisions.

`OrchestratorAudit` hands each event to an `AuditSender`; without one (an
orchestrator no host controller brought up) it records nothing.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..contract.audit import AuditEvent, utc_iso


class _Sink(Protocol):
    def submit(self, event: AuditEvent, producer: str = "orchestrator") -> None: ...


def fingerprint(secret: str) -> str:
    """A token's id for the record: a short SHA-256 prefix, never the token."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


class OrchestratorAudit:
    def __init__(self, sink: _Sink | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._sink = sink
        self._clock = clock

    def _record(self, **fields: Any) -> None:
        if self._sink is None:
            return
        self._sink.submit(AuditEvent(ts_event=utc_iso(self._clock()), **fields))

    # -- auth.* --------------------------------------------------------------

    def token_rejected(self, op: str, *, reason_code: str, src_ip: str = "") -> None:
        """A call with no valid token, or a bottle whose identity token
        doesn't match its source IP."""
        payload: dict[str, Any] = {"op": op, "reason_code": reason_code}
        if src_ip:
            payload["src_ip"] = src_ip
        self._record(type="auth.token_rejected", actor="unauthenticated", action=op,
                     outcome="blocked", sensitivity="security", payload=payload)

    def authz_denied(self, op: str, *, role: str) -> None:
        """A valid token whose role isn't allowed this route."""
        self._record(type="auth.authz_denied", actor=f"role:{role}", action=op,
                     outcome="blocked", sensitivity="security",
                     payload={"op": op, "role": role, "reason_code": "role"})

    def token_minted(self, *, bottle_id: str, token: str, src_ip: str) -> None:
        """A bottle's identity token, minted when it registered."""
        self._record(type="auth.token_minted", actor="role:cli", action="auth.token_minted",
                     outcome="success", sensitivity="security", resource=bottle_id,
                     bottle=bottle_id, bottled_agent=bottle_id,
                     payload={"role": "bottle", "token_id": fingerprint(token),
                              "src_ip": src_ip, "reason_code": "register"})

    # -- decision.* ----------------------------------------------------------

    def proposed(self, *, bottle_id: str, proposal_id: str, tool: str,
                 proposed_file: str, justification: str) -> None:
        """A bottled agent asked the operator for something. Its reason is
        its own claim, so it goes under `untrusted`."""
        self._record(type="decision.proposed", actor=f"bottled-agent:{bottle_id}",
                     action=f"supervise.{tool}", outcome="deferred", sensitivity="security",
                     resource=bottle_id, bottle=bottle_id, bottled_agent=bottle_id,
                     correlation_id=proposal_id,
                     payload={"tool": tool, "proposal_id": proposal_id,
                              "diff_digest": fingerprint(proposed_file)},
                     untrusted={"agent_rationale": justification})

    def resolved(self, *, bottle_id: str, proposal_id: str, decision: str,
                 notes: str) -> None:
        """The operator's answer: approve or modify allows, reject blocks."""
        self._record(type="decision.resolved", actor="role:cli",
                     action=f"supervise.{decision}",
                     outcome="blocked" if decision == "reject" else "allowed",
                     sensitivity="security", resource=bottle_id, bottle=bottle_id,
                     bottled_agent=bottle_id, correlation_id=proposal_id,
                     payload={"proposal_id": proposal_id, "operator_action": decision,
                              "operator_notes": notes})


__all__ = ["OrchestratorAudit", "fingerprint"]
