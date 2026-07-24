"""Cross-cutting utility helpers used by multiple modules.

Top-level (i.e. backend-agnostic) — backend-specific helpers live one
level deeper, under their backend package."""

from __future__ import annotations

import difflib
import hashlib
import ipaddress
import os
import sys


def sha256_hex(content: str) -> str:
    """Hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def render_diff(
    before: str,
    after: str,
    *,
    label: str = "config",
    before_title: str,
    after_label: str,
) -> str:
    """Unified diff of `before` vs `after`, with the two sides headed
    `{label} ({before_title})` and `{label} ({after_label})`. Empty diff (no
    changes) renders as the empty string. The side titles are the caller's —
    e.g. "current"/"proposed" for a supervise proposal."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} ({before_title})",
        tofile=f"{label} ({after_label})",
        lineterm="",
    )
    parts = list(diff)
    if not parts:
        return ""
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts).rstrip("\n")


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
