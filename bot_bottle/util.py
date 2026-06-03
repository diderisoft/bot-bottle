"""Cross-cutting utility helpers used by multiple modules.

Top-level (i.e. backend-agnostic) — backend-specific helpers live one
level deeper, under their backend package."""

from __future__ import annotations

import ipaddress
import os


def is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def expand_tilde(path: str) -> str:
    """Expand a leading '~' to $HOME. Leaves paths without a leading
    tilde unchanged. Falls back to the empty string if $HOME is unset
    (callers should already have checked HOME if they care)."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path
