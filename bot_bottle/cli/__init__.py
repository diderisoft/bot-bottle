"""bot-bottle CLI package.

The subcommand handlers live in `commands/` and are assembled into the
COMMANDS registry by `commands/__init__.py`; the dispatcher `main()` lives
in `__main__.py`. They are re-exported here so `bot_bottle.cli.main`,
`bot_bottle.cli.COMMANDS`, and `bot_bottle.cli.NO_MIGRATION_COMMANDS` stay
importable (the repo-root `cli.py` entry point and the tests use them).
"""

from __future__ import annotations

from .__main__ import main
from .commands import COMMANDS, NO_MIGRATION_COMMANDS

__all__ = ["main", "COMMANDS", "NO_MIGRATION_COMMANDS"]
