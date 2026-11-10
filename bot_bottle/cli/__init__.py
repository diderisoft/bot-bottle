"""bot-bottle CLI package. Re-exports `main` and `COMMANDS` for the `cli.py`
entry point and tests.
"""

from __future__ import annotations

from .__main__ import main
from .commands import COMMANDS

__all__ = ["main", "COMMANDS"]
