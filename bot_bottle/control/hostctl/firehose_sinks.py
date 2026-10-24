"""Configured firehose sinks, each with its own bounded queue and thread. A
sink that falls behind drops its oldest records and reports
`{"firehose_dropped": n}` in its stream.

- `exec`: a command fed JSON lines on stdin, restarted with backoff.
- `https`: NDJSON batches with a bearer token from the secrets file; failed
  batches are dropped and counted.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any

from ...lib.config_store import FirehoseSink
from ...lib.log import warn

QUEUE_LIMIT = 5000
BATCH_RECORDS = 500
FLUSH_SECONDS = 1.0
RESTART_MIN_SECONDS, RESTART_MAX_SECONDS = 1.0, 60.0
TIMEOUT_SECONDS = 10.0


class _Sink:
    def __init__(self, config: FirehoseSink, name: str) -> None:
        self.config = config
        self._queue: deque[dict[str, Any]] = deque()
        self._dropped = 0
        self._cond = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)

    def start(self) -> "_Sink":
        self._thread.start()
        return self

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()

    def offer(self, record: dict[str, Any]) -> None:
        with self._cond:
            if len(self._queue) >= QUEUE_LIMIT:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(record)
            self._cond.notify()

    def _take(self, limit: int, wait: float | None) -> list[dict[str, Any]] | None:
        """Up to `limit` records, a drop marker first if any were lost; None
        once closed."""
        with self._cond:
            if not self._queue and not self._closed:
                self._cond.wait(wait)
            if self._closed:
                return None
            batch = [self._queue.popleft() for _ in range(min(limit, len(self._queue)))]
            if self._dropped:
                batch.insert(0, {"firehose_dropped": self._dropped})
                self._dropped = 0
            return batch

    def lost(self, batch: list[dict[str, Any]]) -> None:
        """A batch that didn't arrive; its records (and any drop) are reported
        with the next."""
        count = sum(int(r.get("firehose_dropped", 0)) if "firehose_dropped" in r else 1
                    for r in batch)
        with self._cond:
            self._dropped += count

    def _run(self) -> None:
        raise NotImplementedError


class ExecSink(_Sink):
    def __init__(self, config: FirehoseSink) -> None:
        super().__init__(config, "firehose-exec")
        self._proc: subprocess.Popen[bytes] | None = None

    def close(self) -> None:
        super().close()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def _spawn(self) -> subprocess.Popen[bytes] | None:
        try:
            return subprocess.Popen(  # pylint: disable=consider-using-with
                list(self.config.command), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        except OSError as e:
            warn(f"firehose: can't start {self.config.command[0]}: {e}")
            return None

    def _run(self) -> None:
        backoff = RESTART_MIN_SECONDS
        stop = threading.Event()
        while True:
            self._proc = self._spawn()
            while self._proc is not None and self._proc.stdin is not None:
                batch = self._take(BATCH_RECORDS, wait=None)
                if batch is None:
                    self._proc.stdin.close()
                    return
                try:
                    self._proc.stdin.write(b"".join(json.dumps(r).encode() + b"\n" for r in batch))
                    self._proc.stdin.flush()
                    backoff = RESTART_MIN_SECONDS
                except (BrokenPipeError, OSError):
                    self.lost(batch)
                    warn(f"firehose: {self.config.command[0]} exited; restarting")
                    break
            if stop.wait(backoff) or self._closed:
                return
            backoff = min(backoff * 2, RESTART_MAX_SECONDS)


class HttpsSink(_Sink):
    def __init__(self, config: FirehoseSink, token: Callable[[], str]) -> None:
        super().__init__(config, "firehose-https")
        self._token = token

    def _post(self, batch: list[dict[str, Any]]) -> None:
        headers = {"Content-Type": "application/x-ndjson"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = b"".join(json.dumps(r).encode() + b"\n" for r in batch)
        req = urllib.request.Request(self.config.url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
            pass

    def _run(self) -> None:
        while True:
            batch = self._take(BATCH_RECORDS, wait=FLUSH_SECONDS)
            if batch is None:
                return
            if not batch:
                continue
            try:
                self._post(batch)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                if isinstance(e, urllib.error.HTTPError):
                    e.close()
                self.lost(batch)


class SinkSet:
    """The running sinks, kept in line with the configured ones."""

    def __init__(self, secret: Callable[[str], str | None]) -> None:
        self._secret = secret
        self._sinks: dict[FirehoseSink, _Sink] = {}
        self._lock = threading.Lock()

    def reconcile(self, configured: tuple[FirehoseSink, ...]) -> None:
        with self._lock:
            for config in set(self._sinks) - set(configured):
                self._sinks.pop(config).close()
            for config in configured:
                if config not in self._sinks:
                    self._sinks[config] = self._make(config).start()

    def _make(self, config: FirehoseSink) -> _Sink:
        if config.type == "exec":
            return ExecSink(config)
        return HttpsSink(config, lambda: (self._secret(config.secret) or "")
                         if config.secret else "")

    def any(self) -> bool:
        with self._lock:
            return bool(self._sinks)

    def offer(self, record: dict[str, Any]) -> None:
        with self._lock:
            sinks = list(self._sinks.values())
        for sink in sinks:
            sink.offer(record)

    def close(self) -> None:
        self.reconcile(())


__all__ = ["ExecSink", "HttpsSink", "SinkSet"]
