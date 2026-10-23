"""Long-running host controller operations as jobs.

A job runs in its own thread with its output redirected to its event list, so
concurrent jobs never interleave. Events are `log` lines followed by one
`result` or `error`; readers can wait for new events and see the whole history.
Jobs live in memory and finished ones are dropped after `RETENTION_SECONDS`.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from ...lib import log

RETENTION_SECONDS = 3600.0

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass
class Job:
    id: str
    kind: str
    owner: str
    state: str = RUNNING
    events: list[dict[str, object]] = field(default_factory=list)
    finished_at: float | None = None
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def record(self, event: dict[str, object], *, final: str | None = None) -> None:
        with self._cond:
            self.events.append({"seq": len(self.events), **event})
            if final is not None:
                self.state = final
                self.finished_at = time.monotonic()
            self._cond.notify_all()

    def summary(self) -> dict[str, object]:
        with self._cond:
            last = self.events[-1] if self.state != RUNNING and self.events else None
            return {"id": self.id, "kind": self.kind, "state": self.state, "final": last}

    def follow(self, start: int = 0, timeout: float | None = None) -> Iterator[dict[str, object]]:
        """Yield events from `start` on, waiting for new ones until the job
        finishes. `timeout` bounds each wait; on expiry the iterator ends."""
        index = start
        while True:
            with self._cond:
                while index >= len(self.events) and self.state == RUNNING:
                    if not self._cond.wait(timeout):
                        return
                pending = self.events[index:]
                done = self.state != RUNNING
            yield from pending
            index += len(pending)
            if done and index >= len(self.events):
                return


class JobRegistry:
    def __init__(self, retention: float = RETENTION_SECONDS) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._retention = retention

    def start(self, kind: str, owner: str, work: Callable[[], dict[str, object]]) -> Job:
        """Run `work` as a job owned by role `owner`. Its return value is the
        `result` event; an exception (including `log.die`) is the `error` event,
        carrying the exception's `job_error_code` as `code` when it has one."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, owner=owner)
        with self._lock:
            self._prune()
            self._jobs[job.id] = job

        def sink(line: str) -> None:
            marker = log.parse_stage(line)
            if marker is None:
                job.record({"type": "log", "line": line})
            else:
                job.record({"type": "stage", "stage": marker[0], "title": marker[1]})

        def run() -> None:
            with log.redirect(sink):
                try:
                    value = work()
                except log.Die as e:
                    job.record({"type": "error", "message": e.message or "failed"}, final=FAILED)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    code = getattr(e, "job_error_code", "")
                    event: dict[str, object] = {
                        "type": "error", "message": str(e) if code else f"{type(e).__name__}: {e}"}
                    if code:
                        event["code"] = code
                    job.record(event, final=FAILED)
                else:
                    job.record({"type": "result", "value": value}, final=SUCCEEDED)

        # A fresh context so this job's sink can't leak into the caller's.
        threading.Thread(target=contextvars.Context().run, args=(run,), daemon=True,
                         name=f"hostctl-job-{job.id}").start()
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        """The job if it exists and `owner` started it."""
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
        return job if job is not None and job.owner == owner else None

    def list(self, owner: str) -> list[Job]:
        with self._lock:
            self._prune()
            return [j for j in self._jobs.values() if j.owner == owner]

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._retention
        for job_id in [i for i, j in self._jobs.items()
                       if j.finished_at is not None and j.finished_at < cutoff]:
            del self._jobs[job_id]


__all__ = ["FAILED", "Job", "JobRegistry", "RETENTION_SECONDS", "RUNNING", "SUCCEEDED"]
