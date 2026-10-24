"""Firecracker backend: Linux KVM microVMs. `FirecrackerBottleBackend` is
re-exported lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import FirecrackerBottleBackend


def __getattr__(name: str) -> Any:
    if name == "FirecrackerBottleBackend":
        from .backend import FirecrackerBottleBackend

        globals()[name] = FirecrackerBottleBackend
        return FirecrackerBottleBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FirecrackerBottleBackend"]
