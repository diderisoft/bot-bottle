"""The request firehose's fan-out in the host controller (PRD 0093).

Batches arrive from the orchestrator and go to every live `audit tail`
subscriber, each through its own bounded queue: a subscriber that falls
behind drops its oldest records and is told how many, and never slows the
others or the gateway. Nothing is stored. When nobody is subscribed the reply
says so, and the gateway stops building records until someone is.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Generator, Mapping
from typing import Any

from ...contract.firehose import Capture

SUBSCRIBER_QUEUE = 1000


def matches(record: Mapping[str, Any], bottle: str, host: str) -> bool:
    """Whether a record passes `tail`'s `--bottle` / `--host` filters. A gap
    marker always does: it belongs to no one bottle."""
    if "firehose_dropped" in record:
        return True
    actor = record.get("actor") or {}
    dst = record.get("dst_endpoint") or {}
    if bottle and actor.get("app_name") != bottle:
        return False
    return not host or dst.get("hostname") == host


class _Subscriber:
    def __init__(self, bottle: str, host: str) -> None:
        self.bottle, self.host = bottle, host
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(SUBSCRIBER_QUEUE)
        self.dropped = 0

    def offer(self, record: dict[str, Any]) -> None:
        if not matches(record, self.bottle, self.host):
            return
        while True:
            try:
                self.queue.put_nowait(record)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass


class FirehoseHub:
    def __init__(self, capture: Callable[[], Capture] | None = None) -> None:
        # What to capture when someone is listening; the settings come later.
        self._capture = capture or Capture
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()

    def wanted(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def reply(self) -> Capture:
        """What the gateway should capture now."""
        if not self.wanted():
            return Capture(wanted=False)
        capture = self._capture()
        return Capture(wanted=True, request_bodies=capture.request_bodies,
                       response_bodies=capture.response_bodies,
                       max_body_bytes=capture.max_body_bytes)

    def publish(self, records: list[dict[str, Any]], dropped: int = 0) -> Capture:
        """Hand a batch to every subscriber. `dropped`: records the senders
        upstream lost, passed on so every consumer sees the gap."""
        with self._lock:
            subscribers = list(self._subscribers)
        gap = [{"firehose_dropped": dropped}] if dropped else []
        for subscriber in subscribers:
            for record in [*gap, *records]:
                subscriber.offer(record)
        return self.reply()

    def subscribe(self, *, bottle: str = "", host: str = "",
                  closed: Callable[[], bool] = lambda: False,
                  poll_seconds: float = 1.0) -> Generator[dict[str, Any] | None]:
        """Records as they arrive, until `closed()` or the caller stops
        iterating. A drop on this subscriber's own queue is reported in line.
        `None` every `poll_seconds` of quiet, so a server writing to a client
        that left notices."""
        subscriber = _Subscriber(bottle, host)
        with self._lock:
            self._subscribers.append(subscriber)
        try:
            while not closed():
                try:
                    record = subscriber.queue.get(timeout=poll_seconds)
                except queue.Empty:
                    yield None
                    continue
                if subscriber.dropped:
                    yield {"firehose_dropped": subscriber.dropped}
                    subscriber.dropped = 0
                yield record
        finally:
            with self._lock:
                self._subscribers.remove(subscriber)


__all__ = ["FirehoseHub", "matches"]
