"""`hostctl.log` rotation: the host controller's stdout and stderr, kept
under `MAX_BYTES` with `KEEP` older files (`hostctl.log.1` the newest).

Whatever starts the host controller (the CLI, systemd or launchd) opens
`hostctl.log` and hands it over as stdout and stderr. Renaming the file
wouldn't move those descriptors, and the service manager can't rotate it. So
the host controller rotates it itself: a thread checks the file's size every
`CHECK_SECONDS`, renames it aside, and opens a fresh `hostctl.log` onto its
own stdout and stderr. Children started after that inherit the new file.

It only touches stdout when that is `hostctl.log`, so a host controller run
by hand on a terminal keeps its terminal.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from ...lib import log

MAX_BYTES = 16 * 1024 * 1024
KEEP = 3
CHECK_SECONDS = 30.0


def _same_file(fd: int, path: Path) -> bool:
    try:
        held, named = os.fstat(fd), path.stat()
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)


def _shift(path: Path, keep: int) -> None:
    """`path` -> `path.1`, `path.1` -> `path.2`, ..., dropping `path.<keep>`."""
    for i in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


class LogRotator:
    """Rotates `path` while it is what `fds` (stdout, stderr) write to."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, keep: int = KEEP,
                 interval: float = CHECK_SECONDS, fds: Sequence[int] = (1, 2)) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._keep = keep
        self._interval = interval
        self._fds = tuple(fds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hostctl-log", daemon=True)

    def start(self) -> "LogRotator":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> bool:
        """Rotate if the log is full; whether it did."""
        fd = self._fds[0]
        if not _same_file(fd, self._path) or os.fstat(fd).st_size < self._max_bytes:
            return False
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        _shift(self._path, self._keep)
        fresh = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            for target in self._fds:
                os.dup2(fresh, target)
        finally:
            os.close(fresh)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check()
            except OSError as e:
                log.warn(f"couldn't rotate {self._path}: {e}")


__all__ = ["CHECK_SECONDS", "KEEP", "LogRotator", "MAX_BYTES"]
