"""Cross-cutting utility helpers used by multiple modules.

Top-level (i.e. backend-agnostic) — backend-specific helpers live one
level deeper, under their backend package."""

from __future__ import annotations

import ipaddress
import os
import re
import sys

from .lib.log import die


def is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def read_tty_line() -> str:
    """Mirror `IFS= read -r REPLY </dev/tty`. Falls back to stdin."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


def expand_tilde(path: str) -> str:
    """Expand a leading '~' to $HOME. Leaves paths without a leading
    tilde unchanged. Falls back to the empty string if $HOME is unset
    (callers should already have checked HOME if they care)."""
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Return a portable bottle identifier from a human-readable name.

    This is deliberately a root utility: names are part of the generic CLI
    and state model, not a Docker container concern.
    """
    if not name:
        die("slugify: missing name")
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        die(f"name '{name}' produced an empty slug; use alphanumeric characters")
    return slug
