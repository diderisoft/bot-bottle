"""The audit-event envelope and hash chain.

Producers build an `AuditEvent`. The host controller's writer seals it: it adds
the chain fields (`epoch`, `seq`, `segment`, `prev`, `hash`),
`ts_recorded`/`ts_mono`, and `producer`, `host` and `engine`, none of which a
producer can set. Only `untrusted` holds agent or remote claims.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = 1

# Orchestrator route the gateway sends events to, relayed to the journal.
AUDIT_EVENTS_PATH = "/audit/events"
GATEWAY_PRODUCERS = frozenset({"egress", "git-gate", "supervise"})

OUTCOMES = frozenset({"allowed", "blocked", "deferred", "success", "failure"})
SENSITIVITIES = ("normal", "security", "restricted")  # ascending


def utc_iso(ts: float) -> str:
    """`ts` in UTC with millisecond precision."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.") + f"{int(ts * 1000) % 1000:03d}Z"


def canonical(record: Mapping[str, Any]) -> str:
    """The serialization every hash is computed over."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def record_hash(prev: str, record: Mapping[str, Any]) -> str:
    """`sha256_hex(prev + canonical(record with hash=""))`. `hash` is present
    and empty, so the key set matches before and after sealing."""
    data = prev + canonical({**record, "hash": ""})
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    """What a producer reports; the writer adds the rest."""

    type: str
    actor: str
    action: str
    outcome: str
    sensitivity: str
    ts_event: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resource: str | None = None
    bottle: str | None = None
    bottled_agent: str | None = None
    activation: str | None = None
    manifest_digest: str | None = None
    correlation_id: str | None = None
    causation_id: str = ""
    payload: dict[str, Any] | None = None
    untrusted: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")
        if self.sensitivity not in SENSITIVITIES:
            raise ValueError(f"unknown sensitivity {self.sensitivity!r}")
        if not self.type or "." not in self.type:
            raise ValueError(f"event type {self.type!r} is not a dotted name")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> "AuditEvent":
        """Strict: unknown keys are refused, so a producer can't add a field
        that looks host-established."""
        known = {f.name for f in fields(cls)}
        unknown = set(body) - known
        if unknown:
            raise ValueError(f"unknown audit event field(s): {sorted(unknown)}")
        return cls(**body)


def truncation_event(producer: str, dropped: int, first: AuditEvent, last: AuditEvent,
                     ts_event: str) -> AuditEvent:
    """The record of events a full delivery queue dropped."""
    return AuditEvent(
        type="audit.truncation_detected", actor=producer, action="audit.truncation_detected",
        outcome="failure", sensitivity="security", ts_event=ts_event,
        payload={"detail": "audit queue overflowed while delivery was failing",
                 "dropped": dropped, "ts_first": first.ts_event, "ts_last": last.ts_event},
    )


__all__ = [
    "AUDIT_EVENTS_PATH",
    "AuditEvent",
    "GATEWAY_PRODUCERS",
    "OUTCOMES",
    "SCHEMA_VERSION",
    "SENSITIVITIES",
    "canonical",
    "record_hash",
    "truncation_event",
    "utc_iso",
]
