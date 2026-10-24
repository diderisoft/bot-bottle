"""Text helpers shared by the host and the services."""

from __future__ import annotations

import difflib
import hashlib


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
    """Unified diff of `before` vs `after`, headed `{label} ({before_title})`
    and `{label} ({after_label})`; "" when unchanged."""
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


__all__ = ["render_diff", "sha256_hex"]
