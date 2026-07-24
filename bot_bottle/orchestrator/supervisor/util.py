"""Pure supervise helpers — diff rendering.

Stateless utility used alongside the `Supervisor` service. Kept as a plain
function (not a method) because it carries no state. (Content hashing is a
generic helper — `bot_bottle.util.sha256_hex`.)
"""

from __future__ import annotations

import difflib


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
