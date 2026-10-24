"""The gateway's audit events, sent to the orchestrator for the journal. Each
component names itself as producer (`egress`, `git-gate`).

`submit` never blocks a request: a background thread drains a bounded queue,
retrying while the orchestrator is unreachable. A full queue drops the oldest,
and an `audit.truncation_detected` event follows once delivery resumes.
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

    def send_now(self, event: AuditEvent) -> bool:
        """Deliver one event now, bypassing the queue, for `audit.delivery:
        required`. True only once written."""
        try:
            self._post(event, required=True)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        return True

    def pending(self) -> int:
        return self._queue.pending()

    def _gap(self, dropped: int, first: AuditEvent, last: AuditEvent) -> AuditEvent:
        return truncation_event(self._producer, dropped, first, last, self.now())

    def _post(self, event: AuditEvent, *, required: bool) -> None:
        """POST one event; raises on any failure or error status."""
        body: dict[str, object] = {"producer": self._producer, "event": event.to_json()}
        if required:
            body["required"] = True
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", **orchestrator_auth_headers()})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
            pass

    def _send(self, event: AuditEvent) -> str:
        try:
            self._post(event, required=False)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                sys.stderr.write(f"{self._producer}: audit event {event.type} refused "
                                 f"(HTTP {e.code}); dropping it\n")
                return DONE
            return RETRY
        except (urllib.error.URLError, TimeoutError, OSError):
            return RETRY
        return DONE


__all__ = ["GatewayAudit", "QUEUE_LIMIT"]
