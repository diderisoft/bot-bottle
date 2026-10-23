"""The gateway's audit events, sent to the orchestrator to relay.

Each gateway component names itself as the producer (`egress`, `git-gate`),
and the orchestrator relays the event to the host controller's journal. The
gateway already calls the orchestrator for every policy lookup, so this needs
no other credential.

`submit` never blocks a request: events go on a bounded queue a background
thread drains, retrying while the orchestrator is unreachable. When the queue
is full the oldest is dropped and an `audit.truncation_detected` event follows
once delivery resumes.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from ..contract.audit import AUDIT_EVENTS_PATH, AuditEvent, truncation_event, utc_iso
from ..lib.retry_queue import DONE, RETRY, RetryQueue
from .policy_resolver import orchestrator_auth_headers

QUEUE_LIMIT = 1000
RETRY_SECONDS = 2.0
TIMEOUT_SECONDS = 5.0


class GatewayAudit:
    def __init__(self, orchestrator_url: str, producer: str, *, limit: int = QUEUE_LIMIT,
                 retry_seconds: float = RETRY_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._url = orchestrator_url.rstrip("/") + AUDIT_EVENTS_PATH
        self._producer = producer
        self._clock = clock
        self._queue: RetryQueue[AuditEvent] = RetryQueue(
            self._send, limit=limit, retry_seconds=retry_seconds, on_gap=self._gap,
            name=f"audit-{producer}")

    def start(self) -> "GatewayAudit":
        self._queue.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        self._queue.close(timeout)

    def now(self) -> str:
        return utc_iso(self._clock())

    def submit(self, event: AuditEvent) -> None:
        self._queue.put(event)

    def pending(self) -> int:
        return self._queue.pending()

    def _gap(self, dropped: int, first: AuditEvent, last: AuditEvent) -> AuditEvent:
        return truncation_event(self._producer, dropped, first, last, self.now())

    def _send(self, event: AuditEvent) -> str:
        body = json.dumps({"producer": self._producer, "event": event.to_json()}).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json", **orchestrator_auth_headers()})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
                return DONE
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                sys.stderr.write(f"{self._producer}: audit event {event.type} refused "
                                 f"(HTTP {e.code}); dropping it\n")
                return DONE
            return RETRY
        except (urllib.error.URLError, TimeoutError, OSError):
            return RETRY


__all__ = ["GatewayAudit", "QUEUE_LIMIT"]
