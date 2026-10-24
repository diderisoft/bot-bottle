"""macOS Apple Container backend (`BOT_BOTTLE_BACKEND=macos-container`).
`MacosContainerBottleBackend` is re-exported lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import MacosContainerBottleBackend


def __getattr__(name: str) -> Any:
    if name == "MacosContainerBottleBackend":
        from .backend import MacosContainerBottleBackend

        globals()[name] = MacosContainerBottleBackend
        return MacosContainerBottleBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MacosContainerBottleBackend"]
