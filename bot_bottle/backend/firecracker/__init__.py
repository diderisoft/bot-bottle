"""Firecracker backend: Linux KVM microVM isolation (issue #342).

Thin by design: `FirecrackerBottleBackend` is re-exported lazily via
`__getattr__`, so importing a leaf module under this package doesn't drag the
backend (and the framework it pulls) into memory.
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
