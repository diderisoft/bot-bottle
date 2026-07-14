"""Active-agent enumeration for the Firecracker backend.

The backend is disabled during the de-sidecar cleanup (#385) — it can't
launch bottles, so there are none to enumerate. Real enumeration returns
with the backend's consolidated relaunch (#354).
"""

from __future__ import annotations

from .. import ActiveAgent


def enumerate_active() -> list[ActiveAgent]:
    return []
