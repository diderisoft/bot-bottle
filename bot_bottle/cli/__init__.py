"""bot-bottle CLI package. Re-exports `main`, `COMMANDS` and
`NO_MIGRATION_COMMANDS` for the `cli.py` entry point and tests.
"""

from __future__ import annotations

from .__main__ import main
from .commands import COMMANDS, NO_MIGRATION_COMMANDS

__all__ = ["main", "COMMANDS", "NO_MIGRATION_COMMANDS"]
