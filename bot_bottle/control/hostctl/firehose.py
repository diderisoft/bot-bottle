"""Request firehose fan-out: batches from the orchestrator go to each `audit
tail` subscriber and sink through its own bounded queue, which drops its oldest
records when behind and reports how many. Nothing is stored. With no consumer,
the reply tells the gateway to stop building records. Settings are reread every
`RELOAD_SECONDS`.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Generator, Mapping
from typing import Any

from ...contract.firehose import Capture
from ...lib.config_store import FirehoseSettings
from .firehose_sinks import SinkSet

SUBSCRIBER_QUEUE = 1000
RELOAD_SECONDS = 5.0


def matches(record: Mapping[str, Any], bottle: str, host: str) -> bool:
    """Whether a record passes `tail`'s `--bottle` / `--host` filters. Gap
    markers always do."""
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
    def __init__(self, settings: Callable[[], FirehoseSettings] | None = None,
                 sinks: SinkSet | None = None) -> None:
        self._read_settings = settings or FirehoseSettings
        self._settings = FirehoseSettings()
        self._read_at = float("-inf")
        self._sinks = sinks
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()

    def _current(self) -> FirehoseSettings:
        """The settings, reread at most every `RELOAD_SECONDS`."""
        if time.monotonic() - self._read_at >= RELOAD_SECONDS:
            self._read_at = time.monotonic()
            try:
                self._settings = self._read_settings()
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # keep the last good settings
            if self._sinks is not None:
                self._sinks.reconcile(self._settings.sinks)
        return self._settings

    def wanted(self) -> bool:
        self._current()
        with self._lock:
            if self._subscribers:
                return True
        return self._sinks is not None and self._sinks.any()

    def reply(self) -> Capture:
        """What the gateway should capture now."""
        if not self.wanted():
            return Capture(wanted=False)
        settings = self._current()
        return Capture(wanted=True, request_bodies=settings.request_bodies,
                       response_bodies=settings.response_bodies,
                       max_body_bytes=settings.max_body_bytes)

    def close(self) -> None:
        if self._sinks is not None:
            self._sinks.close()

    def publish(self, records: list[dict[str, Any]], dropped: int = 0) -> Capture:
        """Hand a batch to every subscriber. `dropped` is upstream loss, passed
        on."""
        with self._lock:
            subscribers = list(self._subscribers)
        gap = [{"firehose_dropped": dropped}] if dropped else []
        for record in [*gap, *records]:
            for subscriber in subscribers:
                subscriber.offer(record)
            if self._sinks is not None:
                self._sinks.offer(record)
        return self.reply()

    def subscribe(self, *, bottle: str = "", host: str = "",
                  closed: Callable[[], bool] = lambda: False,
                  poll_seconds: float = 1.0) -> Generator[dict[str, Any] | None]:
        """Records as they arrive, until `closed()` or the caller stops.
        Own-queue drops are reported in line. Yields None every `poll_seconds`
        of quiet so a server notices a client that left."""
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
