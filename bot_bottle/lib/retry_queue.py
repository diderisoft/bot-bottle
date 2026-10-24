"""A bounded queue a background thread delivers in order, retrying.

`put` never blocks. `send` gets the oldest item and reports whether it's done
(delivered or permanently refused) or should be retried after `retry_seconds`.
A full queue drops the oldest; after the next success `on_gap` gets the count
and the first and last dropped, and may return an item recording the gap.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

DONE = "done"
RETRY = "retry"


class RetryQueue(Generic[T]):
    def __init__(self, send: Callable[[T], str], *, limit: int, retry_seconds: float,
                 on_gap: Callable[[int, T, T], T | None] | None = None,
                 name: str = "retry-queue") -> None:
        self._send = send
        self._limit = limit
        self._retry = retry_seconds
        self._on_gap = on_gap
        self._queue: deque[T] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._dropped = 0
        self._first_dropped: T | None = None
        self._last_dropped: T | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)

    def start(self) -> "RetryQueue[T]":
        self._thread.start()
        return self

    def close(self, timeout: float = 5.0) -> None:
        """Stop after trying to deliver what's queued, for up to `timeout`."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def put(self, item: T) -> None:
        with self._cond:
            if len(self._queue) >= self._limit:
                lost = self._queue.popleft()
                if self._first_dropped is None:
                    self._first_dropped = lost
                self._last_dropped = lost
                self._dropped += 1
            self._queue.append(item)
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def _gap(self) -> T | None:
        if not self._dropped or self._on_gap is None:
            return None
        assert self._first_dropped is not None and self._last_dropped is not None
        marker = self._on_gap(self._dropped, self._first_dropped, self._last_dropped)
        self._dropped, self._first_dropped, self._last_dropped = 0, None, None
        return marker

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue:
                    return
                item = self._queue[0]
            if self._send(item) != DONE:
                with self._cond:
                    if self._closed:
                        return
                    self._cond.wait(self._retry)
                continue
            with self._cond:
                if self._queue and self._queue[0] is item:
                    self._queue.popleft()
                marker = self._gap()
                if marker is not None:
                    self._queue.appendleft(marker)


__all__ = ["DONE", "RETRY", "RetryQueue"]
