"""Cached images a backend keeps on the host, for `bot-bottle cleanup images` (PRD 0088).

Each backend lists its entries with a reason to keep any it must (a current
pin, the current infra tree, an image a container uses). One rule removes the
rest: a superseded release (an old pin, its download) at once, since nothing
asks for it again; a build (an overlay, a local or infra build, a base tree)
once it has gone unused for a while, since the next launch may want it. A cache
hit refreshes an entry's ready marker, so its age is time since last use.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLDER_THAN_DAYS = 14


@dataclass(frozen=True)
class CacheEntry:
    """One cached image: `what` for the listing, its size on disk in bytes,
    when it was last used (epoch seconds), why it stays (None if it may go),
    how to remove it (False when it couldn't be removed), and whether it waits
    out the age cutoff (a build) or goes at once (a superseded release)."""

    what: str
    size: int
    last_used: float
    keep: str | None
    remove: Callable[[], bool]
    build: bool = True


def to_remove(entries: Iterable[CacheEntry], *, older_than_days: float,
              now: float) -> list[CacheEntry]:
    """The entries with no reason to stay: superseded releases, and builds
    unused for `older_than_days`."""
    cutoff = now - older_than_days * 86400
    return [e for e in entries
            if e.keep is None and (not e.build or e.last_used < cutoff)]


def disk_size(path: Path) -> int:
    """Bytes `path` (a file or a tree) takes on disk: allocated blocks, so a
    sparse ext4 counts what it uses, not its nominal size."""
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_blocks * 512
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).lstat().st_blocks * 512
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def mtime(path: Path, default: float = 0.0) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return default


def remove_paths(*paths: Path) -> Callable[[], bool]:
    """A remover for files and trees; True when none is left."""
    def remove() -> bool:
        for path in paths:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.exists():
                shutil.rmtree(path, ignore_errors=True)
        return not any(p.exists() for p in paths)
    return remove


def human(size: int) -> str:
    """`size` bytes as a short figure: 812 MB, 3.4 GB."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000:
            return f"{value:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


__all__ = ["CacheEntry", "DEFAULT_OLDER_THAN_DAYS", "disk_size", "human", "mtime",
           "remove_paths", "to_remove"]
