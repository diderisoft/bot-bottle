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
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from ..contract.audit import AuditEvent, utc_iso
from ..contract.hostctl import AUDIT_APPEND_PATH
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
        self._limit = limit
        self._retry = retry_seconds
        self._clock = clock
        self._queue: deque[_Queued] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._dropped = 0
        self._dropped_range: tuple[str, str] = ("", "")
        self._thread = threading.Thread(target=self._run, daemon=True, name="audit-sender")

    def start(self) -> "AuditSender":
        self._thread.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        """Stop after trying to deliver what's queued, for up to `timeout`."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._thread.join(timeout)

    def submit(self, event: AuditEvent, producer: str = "orchestrator") -> None:
        with self._cond:
            if len(self._queue) >= self._limit:
                lost = self._queue.popleft().event.ts_event
                first = self._dropped_range[0] or lost
                self._dropped_range = (first, lost)
                self._dropped += 1
            self._queue.append(_Queued(event, producer))
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def _truncation_event(self) -> _Queued | None:
        """The record of what overflow dropped, if anything; resets the count."""
        if not self._dropped:
            return None
        first, last = self._dropped_range
        event = AuditEvent(
            type="audit.truncation_detected", actor="orchestrator",
            action="audit.truncation_detected", outcome="failure", sensitivity="security",
            ts_event=utc_iso(self._clock()),
            payload={"detail": "audit queue overflowed while the host controller was "
                               "unreachable", "dropped": self._dropped,
                     "ts_first": first, "ts_last": last},
        )
        self._dropped, self._dropped_range = 0, ("", "")
        return _Queued(event, "orchestrator")

    def _send(self, item: _Queued) -> bool:
        """True once the item needs no more tries: delivered, or refused for good."""
        try:
            status, body = self._relay.request(
                "POST", AUDIT_APPEND_PATH,
                {"producer": item.producer, "event": item.event.to_json()})
        except HostControlUnreachable:
            return False
        if status == 200:
            return True
        if 400 <= status < 500 and status not in (408, 429):
            sys.stderr.write(f"orchestrator: audit event {item.event.type} refused "
                             f"({status}: {body.get('error', '')}); dropping it\n")
            return True
        return False

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue:
                    return
                item = self._queue[0]
            if not self._send(item):
                with self._cond:
                    if self._closed:
                        return
                    self._cond.wait(self._retry)
                continue
            with self._cond:
                if self._queue and self._queue[0] is item:
                    self._queue.popleft()
                truncation = self._truncation_event()
                if truncation is not None:
                    self._queue.appendleft(truncation)


__all__ = ["AuditSender", "QUEUE_LIMIT"]
