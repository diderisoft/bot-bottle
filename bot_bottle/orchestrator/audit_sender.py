"""Hands audit events to the host controller, which owns the journal.

`submit` never blocks the caller: events go on a bounded queue a background
thread drains, in order, into `POST /audit/append`. While the host controller
can't be reached the queue holds them and the thread retries. When the queue
is full the oldest event is dropped, and once delivery resumes an
`audit.truncation_detected` event records how many were lost and the
`ts_event` range they covered, so a gap is itself on the record.

An event the host controller refuses outright (a malformed event, or a
credential-shaped key) is logged and dropped: sending it again can't succeed.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..contract.audit import AuditEvent, truncation_event, utc_iso
from ..contract.hostctl import AUDIT_APPEND_PATH
from ..lib.retry_queue import DONE, RETRY, RetryQueue
from .hostctl_relay import HostControlRelay, HostControlUnreachable

QUEUE_LIMIT = 1000
RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class _Queued:
    event: AuditEvent
    producer: str


class AuditSender:
    def __init__(self, relay: HostControlRelay, *, limit: int = QUEUE_LIMIT,
                 retry_seconds: float = RETRY_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._relay = relay
        self._clock = clock
        self._queue: RetryQueue[_Queued] = RetryQueue(
            self._send, limit=limit, retry_seconds=retry_seconds, on_gap=self._gap,
            name="audit-sender")

    def start(self) -> "AuditSender":
        self._queue.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        self._queue.close(timeout)

    def submit(self, event: AuditEvent, producer: str = "orchestrator") -> None:
        self._queue.put(_Queued(event, producer))

    def pending(self) -> int:
        return self._queue.pending()

    def _gap(self, dropped: int, first: _Queued, last: _Queued) -> _Queued:
        return _Queued(truncation_event("orchestrator", dropped, first.event, last.event,
                                        utc_iso(self._clock())), "orchestrator")

    def _send(self, item: _Queued) -> str:
        try:
            status, body = self._relay.request(
                "POST", AUDIT_APPEND_PATH,
                {"producer": item.producer, "event": item.event.to_json()})
        except HostControlUnreachable:
            return RETRY
        if status == 200:
            return DONE
        if 400 <= status < 500 and status not in (408, 429):
            sys.stderr.write(f"orchestrator: audit event {item.event.type} refused "
                             f"({status}: {body.get('error', '')}); dropping it\n")
            return DONE
        return RETRY


__all__ = ["AuditSender", "QUEUE_LIMIT"]
