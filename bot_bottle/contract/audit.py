"""The audit-event envelope and hash chain (PRD 0089).

Producers (the gateway, the orchestrator, the host controller itself) build an
`AuditEvent`. The host controller's single writer seals it into a journal
record: it adds the chain fields (`epoch`, `seq`, `segment`, `prev`, `hash`),
`ts_recorded`/`ts_mono`, and the attribution it owns (`producer`, `host`,
`engine`). Nothing a producer sends can set those.

Everything outside `untrusted` is host-established; `untrusted` is the one
place an agent's or a remote's claims may go.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

from ..lib.text import sha256_hex


SCHEMA_VERSION = 1

OUTCOMES = frozenset({"allowed", "blocked", "deferred", "success", "failure"})
SENSITIVITIES = ("normal", "security", "restricted")  # ascending


def canonical(record: Mapping[str, Any]) -> str:
    """The one serialization every hash is computed over."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def record_hash(prev: str, record: Mapping[str, Any]) -> str:
    """`sha256_hex(prev + canonical(record with hash=""))`.

    `hash` is present and empty, never omitted, so the key set is the same
    before and after sealing."""
    return sha256_hex(prev + canonical({**record, "hash": ""}))


@dataclass(frozen=True)
class AuditEvent:
    """What a producer reports. The writer adds the rest when it seals it."""

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
        """Strict: an unknown key is refused rather than dropped, so a producer
        can't slip in a top-level field that looks host-established."""
        known = {f.name for f in fields(cls)}
        unknown = set(body) - known
        if unknown:
            raise ValueError(f"unknown audit event field(s): {sorted(unknown)}")
        return cls(**body)


__all__ = [
    "AuditEvent",
    "OUTCOMES",
    "SCHEMA_VERSION",
    "SENSITIVITIES",
    "canonical",
    "record_hash",
]
