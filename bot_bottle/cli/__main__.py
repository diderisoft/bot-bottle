"""Entry point + dispatcher for `python -m bot_bottle.cli`.

Maps `bot-bottle <command>` to its handler in the COMMANDS registry
(`bot_bottle.cli.commands`), enforces the schema-migration gate, and
translates handler exceptions into process exit codes. The repo-root
`cli.py` is the usual way in; this makes the package runnable too, so the
CLI works from an installed copy where there is no `cli.py` on disk.
"""

from __future__ import annotations

import sys

from ..errors import MissingEnvVarError
from ..log import Die, die, error
from ..manifest import ManifestError
from ..orchestrator.store.store_manager import StoreManager
from .commands import COMMANDS, NO_MIGRATION_COMMANDS
from .commands.help import cmd_help


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        cmd_help()
        return 2
    command = argv[0]
    rest = argv[1:]
    if command in ("-h", "--help"):
        cmd_help()
        return 0
    handler = COMMANDS.get(command)
    if handler is None:
        cmd_help()
        die(f"unknown command: {command}")
    mgr = StoreManager.instance()
    if command not in NO_MIGRATION_COMMANDS and not mgr.is_migrated():
        sys.stderr.write("bot-bottle: database schema is out of date\n")
        sys.stderr.write("Migrate now? [y/N] ")
        sys.stderr.flush()
        try:
            answer = sys.stdin.readline().strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            error("migration required — re-run and confirm to migrate")
            return 1
        mgr.migrate()
    try:
        return handler(rest) or 0
    except MissingEnvVarError as e:
        error(str(e))
        return 1
    except ManifestError as e:
        error(str(e))
        return 1
    except Die as e:
        return e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
