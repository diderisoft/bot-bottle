"""The orchestrator's audit events: who it admitted and refused, identity
tokens minted, and supervise proposals and decisions. Sent via an
`AuditSender`; without one (no host controller) nothing is recorded.

Producers name a bottle by slug; the orchestrator and gateway know it by
registry id, which is the run's `activation`. Each event leaves with both.
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


# The most of a decision's policy diff its `decision.resolved` carries. A
# longer one is cut, flagged `diff_truncated`; `diff_digest` names it whole.
DIFF_MAX_BYTES = 64 * 1024


def capped_diff(diff: str) -> dict[str, Any]:
    """`decision.resolved`'s diff fields: the diff, cut to DIFF_MAX_BYTES of
    UTF-8, its fingerprint, and whether it was cut. Empty for no diff."""
    if not diff:
        return {}
    raw = diff.encode("utf-8")
    cut = len(raw) > DIFF_MAX_BYTES
    return {"diff": raw[:DIFF_MAX_BYTES].decode("utf-8", "ignore") if cut else diff,
            "diff_digest": fingerprint(diff), "diff_truncated": cut}


# Registry id -> slug, remembered so late events still get a slug.
_SLUG_CACHE_LIMIT = 10_000


class OrchestratorAudit:
    def __init__(self, sink: _Sink | None = None, *,
                 slug_for: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._sink = sink
        self._slug_for = slug_for
        self._slugs: dict[str, str] = {}
        self._clock = clock

    def slug(self, registry_id: str) -> str | None:
        """The bottle's slug for a registry id; None when unknown."""
        if registry_id in self._slugs:
            return self._slugs[registry_id]
        slug = self._slug_for(registry_id) if self._slug_for is not None else None
        if slug:
            if len(self._slugs) >= _SLUG_CACHE_LIMIT:
                self._slugs.clear()
            self._slugs[registry_id] = slug
        return slug

    def named(self, event: AuditEvent) -> AuditEvent:
        """`event` with its bottle named by slug and the registry id as
        `activation`; unchanged for an unknown id."""
        registry_id = event.bottle
        slug = self.slug(registry_id) if registry_id else None
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
        """Pass on a gateway component's event, named by slug. `required`
        delivers now and reports whether it was written (never, without a host
        controller)."""
        if self._sink is None:
            return not required
        if required:
            return self._sink.send_now(self.named(event), producer)
        self._sink.submit(self.named(event), producer)
        return True

    def _record(self, *, required: bool = False, **fields: Any) -> bool:
        event = AuditEvent(ts_event=utc_iso(self._clock()), **fields)
        return self.relay(event, producer="orchestrator", required=required)

    # --- auth.* ---

    def token_rejected(self, op: str, *, reason_code: str, src_ip: str = "") -> None:
        """A call with no valid token, or an identity token that doesn't match
        its source IP."""
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

    # --- decision.* ---

    def proposed(self, *, bottle_id: str, proposal_id: str, tool: str,
                 proposed_file: str, justification: str) -> None:
        """An agent asked the operator for something; its reason is a claim, so
        it goes under `untrusted`."""
        self._record(type="decision.proposed", actor=f"bottled-agent:{bottle_id}",
                     action=f"supervise.{tool}", outcome="deferred", sensitivity="security",
                     resource=bottle_id, bottle=bottle_id, bottled_agent=bottle_id,
                     correlation_id=proposal_id,
                     payload={"tool": tool, "proposal_id": proposal_id,
                              "diff_digest": fingerprint(proposed_file)},
                     untrusted={"agent_rationale": justification})

    def resolved(self, *, bottle_id: str, proposal_id: str, decision: str,
                 notes: str, diff: str = "", required: bool = False) -> bool:
        """The operator's answer: approve or modify allows, reject blocks.
        `diff` is the policy change it applies, rendered; the journal is its
        only record (`capped_diff`). `required` records before applying;
        False means it wasn't recorded."""
        return self._record(required=required, type="decision.resolved", actor="role:cli",
                     action=f"supervise.{decision}",
                     outcome="blocked" if decision == "reject" else "allowed",
                     sensitivity="security", resource=bottle_id, bottle=bottle_id,
                     bottled_agent=bottle_id, correlation_id=proposal_id,
                     payload={"proposal_id": proposal_id, "operator_action": decision,
                              "operator_notes": notes, **capped_diff(diff)})

    def answered_late(self, *, bottle_id: str, proposal_id: str, decision: str,
                      notes: str, first: str) -> None:
        """An answer to a proposal that already had one (`first`), refused:
        the first stands. Kept so both answers, and where each came from
        (`notes`), are on record."""
        self._record(type="decision.late", actor="role:cli", action=f"supervise.{decision}",
                     outcome="failure", sensitivity="security", resource=bottle_id,
                     bottle=bottle_id, bottled_agent=bottle_id, correlation_id=proposal_id,
                     payload={"proposal_id": proposal_id, "operator_action": decision,
                              "operator_notes": notes, "decided_as": first,
                              "reason_code": "already_answered"})

    # --- canary.* ---

    def canary_fired(self, *, bottle_id: str, event_id: str, reason: str,
                     request_path: str) -> None:
        """The gateway detected a canary hit on this bottle."""
        self._record(type="canary.fired", actor=f"bottled-agent:{bottle_id}",
                     action="canary.fired", outcome="blocked", sensitivity="security",
                     resource=bottle_id, bottle=bottle_id, bottled_agent=bottle_id,
                     correlation_id=event_id,
                     payload={"event_id": event_id, "reason": reason,
                              "request_path": request_path})

    def canary_resolved(self, *, bottle_id: str, event_id: str, action: str,
                        notes: str) -> None:
        """The operator resolved the canary event (acknowledged or terminated)."""
        self._record(type="canary.resolved", actor="role:cli",
                     action=f"canary.{action}",
                     outcome="allowed" if action == "acknowledge" else "blocked",
                     sensitivity="security", resource=bottle_id, bottle=bottle_id,
                     bottled_agent=bottle_id, correlation_id=event_id,
                     payload={"event_id": event_id, "operator_action": action,
                              "operator_notes": notes})


__all__ = ["DIFF_MAX_BYTES", "OrchestratorAudit", "capped_diff", "fingerprint"]
