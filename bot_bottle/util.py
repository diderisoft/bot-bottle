"""Backend-agnostic helpers."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone

from .lib.log import die


def read_tty_line() -> str:
    """Like `IFS= read -r REPLY </dev/tty`; falls back to stdin."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


def expand_tilde(path: str) -> str:
    """Expand a leading '~' to $HOME ("" if HOME is unset)."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A portable bottle identifier from a human-readable name."""
    if not name:
        die("slugify: missing name")
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        die(f"name '{name}' produced an empty slug; use alphanumeric characters")
    return slug


def same_instant(a: str, b: str) -> bool:
    """Whether two ISO timestamps are the same instant, allowing for precision
    and zone formatting lost in a round trip."""
    def parse(v: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    left, right = parse(a), parse(b)
    return left is not None and right is not None and abs((left - right).total_seconds()) < 1
