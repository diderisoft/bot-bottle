"""The code a host controller runs, so a CLI can spot a stale daemon."""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def build_id(package_root: Path = PACKAGE_ROOT) -> str:
    """Short hash over every `.py` file under `package_root` (path and bytes)."""
    h = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(path.relative_to(package_root).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


@cache
def current_build_id() -> str:
    """`build_id()` of this process's package, computed once."""
    return build_id()


__all__ = ["PACKAGE_ROOT", "build_id", "current_build_id"]
