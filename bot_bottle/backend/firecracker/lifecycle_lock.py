"""Serialize Firecracker run-directory creation with orphan cleanup."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from . import util


def _lock_path() -> Path:
    return util.env_dir() / "run.lifecycle.lock"


@contextmanager
def hold() -> Generator[None]:
    """Hold off cleanup while a launch dir has no VMM yet."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


__all__ = ["hold"]
