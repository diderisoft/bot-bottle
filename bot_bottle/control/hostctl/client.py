"""The CLI's client for the host controller's routes."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator


from ...contract.firehose import FIREHOSE_STREAM_PATH
from ...contract.hostctl import JOBS_PATH, bearer
from .runner import HostControlError, HostControlState


class HostControlClient:
    def __init__(self, state: HostControlState, token: str) -> None:
        self._base = state.url
        self._headers = bearer(token)

    def _open(self, path: str, timeout: float | None,  # type: ignore[no-untyped-def]
              body: dict[str, object] | None = None, method: str | None = None):
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self._headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._base + path, data=data, headers=headers,
                                     method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise HostControlError(f"host controller: {e.code} on {path}: {detail}") from None
        except OSError as e:
            raise HostControlError(f"host controller unreachable: {e}") from None

    def get(self, path: str) -> dict[str, object]:
        with self._open(path, timeout=10) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        with self._open(path, timeout=30, body=body) as resp:
            return json.loads(resp.read())

    def delete(self, path: str) -> dict[str, object]:
        with self._open(path, timeout=30, method="DELETE") as resp:
            return json.loads(resp.read())

    def jobs(self) -> list[dict[str, object]]:
        return list(self.get(JOBS_PATH)["jobs"])  # type: ignore[arg-type]

    def events(self, job_id: str, start: int = 0) -> Iterator[dict[str, object]]:
        """A job's events from `start` until it finishes. No read timeout:
        a build step can be quiet for minutes."""
        with self._open(f"{JOBS_PATH}/{job_id}/events?from={start}", timeout=None) as resp:
            for line in resp:
                if line.strip():
                    yield json.loads(line)

    def firehose(self, *, bottle: str = "", host: str = "") -> Iterator[dict[str, object]]:
        """Request records as they arrive, until the caller stops."""
        query = urllib.parse.urlencode({k: v for k, v in (("bottle", bottle), ("host", host)) if v})
        path = FIREHOSE_STREAM_PATH + (f"?{query}" if query else "")
        with self._open(path, timeout=None) as resp:
            for line in resp:
                if line.strip():  # blank lines are the server's keep-alives
                    yield json.loads(line)

    def follow(self, job_id: str, write: Callable[[str], None],
               stage: Callable[[str, str], None] | None = None) -> dict[str, object] | None:
        """Write a job's log lines as they arrive (and pass its `stage`
        events to ``stage``), and return its final (`result` or `error`)
        event. On Ctrl-C, return None; the job keeps running."""
        try:
            for event in self.events(job_id):
                if event.get("type") == "log":
                    write(str(event.get("line", "")))
                elif event.get("type") == "stage" and stage is not None:
                    stage(str(event.get("stage", "")), str(event.get("title", "")))
                elif event.get("type") in ("result", "error"):
                    return event
        except KeyboardInterrupt:
            return None
        raise HostControlError(f"host controller: job {job_id} ended without a result")


__all__ = ["HostControlClient"]
