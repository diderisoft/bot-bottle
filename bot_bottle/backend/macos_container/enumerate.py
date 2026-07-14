"""Active-agent enumeration for the macOS Apple Container backend.

The backend is disabled during the companion-container removal (#385) — it can't
launch bottles, so there are none to enumerate. Enumeration returns when
the backend grows the consolidated gateway.
"""

from __future__ import annotations

from .. import ActiveAgent


def enumerate_active() -> list[ActiveAgent]:
    return []
