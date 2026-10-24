"""Egress audit events. Blocks are blocked `egress.decision`s, DLP hits
`egress.anomaly`s, and an allowed request is recorded only on a bottle's first
contact with its `host:port`.

Under `audit.delivery: required` an allowed action's event is sent immediately,
and False means the caller must refuse. That send blocks, so async callers run
it off the event loop.
"""

from __future__ import annotations

import typing

from ...contract.audit import AuditEvent
from ...contract.blocks import REASON_CODE_KEY

# First contacts remembered before starting over.
CONTACTED_LIMIT = 50_000
# 403 body when a `required` bottle's event couldn't be written.
AUDIT_REFUSED = "egress: refused because its audit record could not be written"


class Sink(typing.Protocol):
    def now(self) -> str: ...

    def submit(self, event: AuditEvent) -> None: ...

    def send_now(self, event: AuditEvent) -> bool: ...


def egress_event(flow: typing.Any, slug: str, kind: str, outcome: str, *, reason: str,
                 now: str, code: str = "") -> AuditEvent:
    """The event for one decision about `flow` from bottle `slug`. A block
    carries its `code` (contract.blocks) for the session status bar."""
    conn = flow.client_conn
    src_ip = conn.peername[0] if conn is not None and conn.peername else ""
    host, port, method = flow.request.pretty_host, flow.request.port, flow.request.method
    payload: dict[str, object] = {"method": method, "host": host, "port": port,
                                  "src_ip": src_ip}
    if reason:
        payload["reason"] = reason
    if code:
        payload[REASON_CODE_KEY] = code
    return AuditEvent(
        type=kind, actor=f"bottled-agent:{slug}" if slug else "unattributed",
        action=f"egress.{method.lower()}", outcome=outcome, sensitivity="security",
        ts_event=now, resource=f"{host}:{port}",
        bottle=slug or None, bottled_agent=slug or None, payload=payload,
    )


class EgressAudit:
    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._contacted: set[tuple[str, str, int]] = set()

    def record(self, flow: typing.Any, slug: str, kind: str, outcome: str,
               reason: str = "", *, required: bool = False, code: str = "") -> bool:
        """Record one decision; `required` sends now and reports whether it was
        written."""
        event = egress_event(flow, slug, kind, outcome, reason=reason, now=self._sink.now(),
                             code=code)
        if required:
            return self._sink.send_now(event)
        self._sink.submit(event)
        return True

    def first_contact(self, flow: typing.Any, slug: str, *, required: bool) -> bool:
        """Record an allowed request on `slug`'s first contact with its host.
        False when `required` and unwritten; the host isn't remembered then."""
        key = (slug, flow.request.pretty_host, flow.request.port)
        if key in self._contacted:
            return True
        if not self.record(flow, slug, "egress.decision", "allowed", required=required):
            return False
        if len(self._contacted) >= CONTACTED_LIMIT:
            self._contacted.clear()  # bounded; a host may be recorded again
        self._contacted.add(key)
        return True


__all__ = ["AUDIT_REFUSED", "CONTACTED_LIMIT", "EgressAudit", "egress_event"]
