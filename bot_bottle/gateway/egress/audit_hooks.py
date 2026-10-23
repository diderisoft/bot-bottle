"""The egress proxy's audit events, kept out of the mitmproxy adapter.

Every block is a blocked `egress.decision` and every DLP hit an
`egress.anomaly`. An allowed request is recorded only the first time a bottle
reaches its `host:port`, so the journal shows everywhere an agent went without
one record per request.

Under a bottle's `audit.delivery: required` an allowed action's event is sent
now rather than queued, and `False` means it wasn't written, so the caller
must refuse the action. That send blocks, so an async caller runs it off the
event loop. A refusal is never held: nothing goes out either way.
"""

from __future__ import annotations

import typing

from ...contract.audit import AuditEvent

# Most (bottle, host, port) first contacts remembered before starting over.
CONTACTED_LIMIT = 50_000
# The 403 body when a `required` bottle's audit event couldn't be written.
AUDIT_REFUSED = "egress: refused because its audit record could not be written"


class Sink(typing.Protocol):
    def now(self) -> str: ...

    def submit(self, event: AuditEvent) -> None: ...

    def send_now(self, event: AuditEvent) -> bool: ...


def egress_event(flow: typing.Any, slug: str, kind: str, outcome: str, *, reason: str,
                 now: str) -> AuditEvent:
    """The event for one decision about `flow`, from the bottle `slug`."""
    conn = flow.client_conn
    src_ip = conn.peername[0] if conn is not None and conn.peername else ""
    host, port, method = flow.request.pretty_host, flow.request.port, flow.request.method
    payload: dict[str, object] = {"method": method, "host": host, "port": port,
                                  "src_ip": src_ip}
    if reason:
        payload["reason"] = reason
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
               reason: str = "", *, required: bool = False) -> bool:
        """Record one decision; `required` sends it now and says whether it
        was written."""
        event = egress_event(flow, slug, kind, outcome, reason=reason, now=self._sink.now())
        if required:
            return self._sink.send_now(event)
        self._sink.submit(event)
        return True

    def first_contact(self, flow: typing.Any, slug: str, *, required: bool) -> bool:
        """Record an allowed request the first time `slug` reaches its host.
        False when `required` and the record couldn't be written; the host
        isn't remembered then, so the next request tries again."""
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
