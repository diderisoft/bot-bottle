"""Entry point + dispatcher for `python -m bot_bottle.cli`.

Maps `bot-bottle <command>` to its handler in the COMMANDS registry
(`bot_bottle.cli.commands`), enforces the schema-migration gate, and
translates handler exceptions into process exit codes. The repo-root
`cli.py` is the usual way in; this makes the package runnable too, so the
CLI works from an installed copy where there is no `cli.py` on disk.
"""

from __future__ import annotations

import os
import sys

from ..errors import MissingEnvVarError, SecretsFileError
from ..lib.log import Die, die, error
from ..manifest import ManifestError, ManifestIndex
from ..orchestrator.store.store_manager import StoreManager
from ..paths import host_db_path
from .commands import COMMANDS, NO_MIGRATION_COMMANDS
from .commands.help import cmd_help


def _is_agent(name: str) -> bool:
    try:
        return name in ManifestIndex.resolve(os.getcwd()).all_agent_names
    except ManifestError:
        return False


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-return-statements
    if argv is None:
        argv = sys.argv[1:]
    command = argv[0] if argv else ""
    rest = argv[1:]
    if command in ("-h", "--help"):
        cmd_help()
        return 0
    handler = COMMANDS.get(command)
    if handler is None and (not command or command.startswith("-") or _is_agent(command)):
        # Bare `bot-bottle`, `bot-bottle <agent>` or `bot-bottle -v` starts an
        # agent on this directory.
        command, handler, rest = "quick", COMMANDS["quick"], argv
    if handler is None:
        cmd_help()
        die(f"unknown command or agent: {command}")
    mgr = StoreManager(host_db_path())
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
    except (MissingEnvVarError, SecretsFileError) as e:
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
