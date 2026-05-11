"""Cross-cutting utility helpers used by multiple modules.

Top-level (i.e. backend-agnostic) — Docker-specific helpers live in
claude_bottle/backend/docker/util.py."""

from __future__ import annotations

import os


def expand_tilde(path: str) -> str:
    """Expand a leading '~' to $HOME. Leaves paths without a leading
    tilde unchanged. Falls back to the empty string if $HOME is unset
    (callers should already have checked HOME if they care)."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path
