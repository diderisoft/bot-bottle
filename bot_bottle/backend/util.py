"""Cross-backend utility helpers — host-side primitives shared by
every backend implementation. Backend-specific helpers live one level
deeper (e.g. bot_bottle/backend/docker/util.py)."""

from __future__ import annotations

import os

from ..log import die


def host_skill_dir(name: str) -> str:
    """Return the host-side path for a named skill:
    `$HOME/.claude/skills/<name>`. Dies if HOME is unset."""
    home = os.environ.get("HOME")
    if not home:
        die("HOME not set")
    return f"{home}/.claude/skills/{name}"
