"""The orchestrator's audit events: who it let in and turned away, the
identity tokens it minted, and supervise proposals and their decisions.

`OrchestratorAudit` hands each event to an `AuditSender`; without one (an
orchestrator no host controller brought up) it records nothing.

Every producer names a bottle by its slug (`amber-fox`), but the orchestrator
and gateway know it by its registry id, minted afresh at each registration.
That id is the run's `activation`, the one the host controller records too.
Each event is renamed on its way out (`bottle` the slug, `activation` the
registry id), so a bottle's events from every producer share both names.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..contract.audit import AuditEvent, utc_iso


class _Sink(Protocol):
    def submit(self, event: AuditEvent, producer: str = "orchestrator") -> None: ...

    def send_now(self, event: AuditEvent, producer: str = "orchestrator") -> bool: ...


def fingerprint(secret: str) -> str:
    """A token's id for the record: a short SHA-256 prefix, never the token."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


# Registry id -> slug lookups remembered, so an event that arrives after its
# bottle deregistered is still named by slug.
_SLUG_CACHE_LIMIT = 10_000


class OrchestratorAudit:
    def __init__(self, sink: _Sink | None = None, *,
                 slug_for: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._sink = sink
        self._slug_for = slug_for
        self._slugs: dict[str, str] = {}
        self._clock = clock

    def _slug(self, registry_id: str) -> str | None:
        if registry_id in self._slugs:
            return self._slugs[registry_id]
        slug = self._slug_for(registry_id) if self._slug_for is not None else None
        if slug:
            if len(self._slugs) >= _SLUG_CACHE_LIMIT:
                self._slugs.clear()
            self._slugs[registry_id] = slug
        return slug

    def named(self, event: AuditEvent) -> AuditEvent:
        """`event` with its bottle named by slug and the registry id as its
        `activation`. Unchanged when the id isn't a known bottle."""
        registry_id = event.bottle
        slug = self._slug(registry_id) if registry_id else None
        if not slug or slug == registry_id:
            return event
        body = event.to_json()
        body["activation"] = event.activation or registry_id
        for name in ("bottle", "bottled_agent", "resource"):
            if body[name] == registry_id:
                body[name] = slug
        if event.actor == f"bottled-agent:{registry_id}":
            body["actor"] = f"bottled-agent:{slug}"
        return AuditEvent.from_json(body)

    def relay(self, event: AuditEvent, *, producer: str, required: bool = False) -> bool:
        """Pass on an event a gateway component produced, named by slug.
        `required`: deliver it now and say whether it was written; without a
        host controller to write it, it wasn't."""
        if self._sink is None:
            return not required
        if required:
            return self._sink.send_now(self.named(event), producer)
        self._sink.submit(self.named(event), producer)
        return True

    def _record(self, *, required: bool = False, **fields: Any) -> bool:
        event = AuditEvent(ts_event=utc_iso(self._clock()), **fields)
        return self.relay(event, producer="orchestrator", required=required)

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
                 notes: str, required: bool = False) -> bool:
        """The operator's answer: approve or modify allows, reject blocks.
        `required`: recorded before it's applied; False means it wasn't."""
        return self._record(required=required, type="decision.resolved", actor="role:cli",
                     action=f"supervise.{decision}",
                     outcome="blocked" if decision == "reject" else "allowed",
                     sensitivity="security", resource=bottle_id, bottle=bottle_id,
                     bottled_agent=bottle_id, correlation_id=proposal_id,
                     payload={"proposal_id": proposal_id, "operator_action": decision,
                              "operator_notes": notes})


__all__ = ["OrchestratorAudit", "fingerprint"]
