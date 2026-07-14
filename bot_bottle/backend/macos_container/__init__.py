"""macOS Apple Container backend.

Selectable via `BOT_BOTTLE_BACKEND=macos-container`. This package owns
the Apple `container` CLI integration; launch remains gated until the
gateway network enforcement shape is implemented.
"""

from .backend import MacosContainerBottleBackend

__all__ = ["MacosContainerBottleBackend"]
