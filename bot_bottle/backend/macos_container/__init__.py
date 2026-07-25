"""macOS Apple Container backend.

Selectable via `BOT_BOTTLE_BACKEND=macos-container`. This package owns the Apple
`container` CLI integration; launch remains gated until the gateway network
enforcement shape is implemented.

Thin by design: `MacosContainerBottleBackend` is re-exported lazily via
`__getattr__`, so importing a leaf module under this package doesn't drag the
backend (and the framework it pulls) into memory.
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
