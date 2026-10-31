"""Entry point and dispatcher for `python -m bot_bottle.cli`: maps `bot-bottle
<command>` to its handler, enforces the migration gate, and turns exceptions
into exit codes.
"""

from __future__ import annotations

import os
import sys

from ..environment import InvalidEnvironmentError, active_name, ensure_active_registered
from ..errors import MissingEnvVarError, SecretsFileError
from ..lib.log import Die, die, error
from ..manifest import ManifestError, ManifestIndex
from ..orchestrator.store.store_manager import StoreManager
from ..paths import host_db_path
from .commands import COMMANDS, NO_MIGRATION_COMMANDS, QUICK
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
    try:
        active_name()  # a bad BOT_BOTTLE_ENV stops everything, before any path
    except InvalidEnvironmentError as e:
        error(str(e))
        return 2
    if command in ("-h", "--help"):
        cmd_help()
        return 0
    handler = COMMANDS.get(command)
    if handler is None and (not command or command.startswith("-") or _is_agent(command)):
        # Bare `bot-bottle`, `bot-bottle <agent>` or `bot-bottle -v` starts an
        # agent on this directory.
        command, handler, rest = "quick", QUICK, argv
    if handler is None:
        cmd_help()
        die(f"unknown command or agent: {command}")
    try:
        # A named environment registers on first use; `envs` manages them
        # and must not register the one it removes.
        if command != "envs":
            ensure_active_registered()
    except InvalidEnvironmentError as e:
        error(str(e))
        return 2
    mgr = StoreManager(host_db_path())
    fresh = mgr.is_fresh()  # before is_migrated, which creates the file
    if command in NO_MIGRATION_COMMANDS or mgr.is_migrated():
        pass
    elif fresh:
        mgr.migrate()  # a new install: nothing to migrate from, so don't ask
    else:
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
