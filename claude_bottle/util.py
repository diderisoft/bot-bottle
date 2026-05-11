"""Cross-cutting utility helpers used by multiple modules.

Top-level (i.e. backend-agnostic) — Docker-specific helpers live in
claude_bottle/backend/docker/util.py."""

from __future__ import annotations

import os
import re


def expand_tilde(path: str) -> str:
    """Expand a leading '~' to $HOME. Leaves paths without a leading
    tilde unchanged. Falls back to the empty string if $HOME is unset
    (callers should already have checked HOME if they care)."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path


_IPV4_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")


def is_ipv4_literal(s: str) -> bool:
    """True iff `s` looks like a dotted-quad IPv4 literal. Does not
    validate octet ranges; consumers that care about that should run
    a stricter check. Empty input returns False."""
    if not s:
        return False
    return bool(_IPV4_RE.match(s))
