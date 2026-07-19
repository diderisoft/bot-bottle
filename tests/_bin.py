"""Resolved paths to system binaries used by subprocess-based tests.

NixOS and other non-FHS hosts don't populate ``/bin`` (there is no
``/bin/sleep``), so tests that spawn real short-lived helper processes
must resolve the binary from ``PATH`` rather than hardcoding an FHS path.
Import the resolved constant (e.g. ``SLEEP``) instead of writing
``/bin/sleep`` inline.
"""

from __future__ import annotations

import shutil


def resolve(name: str, fallback: str) -> str:
    """Absolute path to ``name`` from ``PATH``; ``fallback`` on FHS hosts
    where the binary isn't on ``PATH`` but lives at a known ``/bin`` path."""
    return shutil.which(name) or fallback


# Real ``sleep`` binary. ``/bin/sleep`` is absent on NixOS; resolve from
# PATH so subprocess tests run instead of erroring with FileNotFoundError.
SLEEP = resolve("sleep", "/bin/sleep")
