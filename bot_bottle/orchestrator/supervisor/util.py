"""Pure supervise helpers — diff rendering and content hashing.

Stateless utilities used alongside the `Supervisor` service. Kept as plain
functions (not methods) because they carry no state and are also handy to
callers that only need to hash or diff.
"""

from __future__ import annotations

import difflib
import hashlib


def render_diff(before: str, after: str, *, label: str = "config") -> str:
    """Unified diff suitable for the audit log + TUI. Empty diff (no changes)
    renders as the empty string."""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} (current)",
        tofile=f"{label} (proposed)",
        lineterm="",
    )
    parts = list(diff)
    if not parts:
        return ""
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts).rstrip("\n")


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
