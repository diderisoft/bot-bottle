"""Delete transcript copies once they're older than the host's retention
(PRD 0095): `bot-bottle audit transcript-retention`, default 30 days.

The sweep runs when the host controller starts and daily after. A run's
copies go together, with their rows (`TranscriptStore`), once the run is
closed and nothing in it changed for the retention period; each deletion is
recorded as `transcript.deleted` with the copies' digests, so the journal
still says what was there.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib import log
from ...paths import host_db_path
from .transcripts import TranscriptStore, transcripts_root

if TYPE_CHECKING:
    from .audit_events import HostAudit

SWEEP_SECONDS = 86400.0


def _digest(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def _last_change(run: Path) -> float:
    return max((p.stat().st_mtime for p in run.iterdir()), default=run.stat().st_mtime)


def sweep(audit: "HostAudit", retention_days: int, *, now: float | None = None) -> list[Path]:
    """Delete closed runs unchanged for `retention_days`; the runs deleted."""
    root = transcripts_root()
    store = TranscriptStore(host_db_path())
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    deleted: list[Path] = []
    for recorded in store.runs():
        if not recorded["closed"]:
            continue
        run = root / recorded["session"] / recorded["activation"]
        try:
            if not run.is_dir() or _last_change(run) >= cutoff:
                continue
            copies: list[dict[str, Any]] = []
            for copy in sorted(run.glob("*.jsonl")):
                size, sha = _digest(copy)
                copies.append({"copy": str(copy.relative_to(root)), "bytes": size,
                               "sha256": sha})
            shutil.rmtree(run)
            store.forget_run(recorded["session"], recorded["activation"])
        except OSError as e:
            log.warn(f"transcripts: couldn't delete {run}: {e}")
            continue
        deleted.append(run)
        audit.transcript_deleted(bottle=run.parent.name, activation=run.name, copies=copies,
                                 retention_days=retention_days)
        if run.parent.is_dir() and not any(run.parent.iterdir()):
            run.parent.rmdir()
    return deleted


def _retention_days() -> int:
    # pylint: disable=import-outside-toplevel
    from ...lib.config_store import ConfigStore
    return ConfigStore(host_db_path()).transcript_retention_days()


class RetentionSweeper:
    """Sweeps at start and every SWEEP_SECONDS until stopped."""

    def __init__(self, audit: "HostAudit", *,
                 retention_days: Callable[[], int] = _retention_days,
                 interval: float = SWEEP_SECONDS) -> None:
        self._audit = audit
        self._retention_days = retention_days
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="transcript-retention",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop, waiting for a sweep in progress, so nothing writes the host DB
        after the host controller (or a test) has moved on."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            try:
                sweep(self._audit, self._retention_days())
            except Exception as e:  # pylint: disable=broad-exception-caught  # try again tomorrow
                log.warn(f"transcripts: retention sweep failed: {e}")
            if self._stop.wait(self._interval):
                return


__all__ = ["RetentionSweeper", "SWEEP_SECONDS", "sweep"]
