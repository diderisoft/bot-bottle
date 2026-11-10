"""Entry point and dispatcher for `python -m bot_bottle.cli`: maps `bot-bottle
<command>` to its handler and turns exceptions into exit codes.
"""

from __future__ import annotations

import os
import sys

from ..environment import (
    InvalidEnvironmentError, active_name, ensure_active_registered, root_owned_fix,
    root_owned_state, sudo_invoker,
)
from ..errors import MissingEnvVarError, SecretsFileError
from ..lib.log import Die, die, error
from ..manifest import ManifestError, ManifestIndex
from .commands import COMMANDS, HOME, QUICK
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
    if not argv:
        # Bare `bot-bottle`: home (a menu on a terminal, else `status`).
        command, handler, rest = "home", HOME, []
    elif handler is None and (command.startswith("-") or _is_agent(command)):
        # `bot-bottle <agent>` or `bot-bottle -v` starts an agent on this
        # directory.
        command, handler, rest = "quick", QUICK, argv
    if handler is None:
        cmd_help()
        die(f"unknown command or agent: {command}")
    # Under sudo only the backend's root-owned pieces are written: registering
    # here would leave per-user state the user can't write.
    as_sudo = sudo_invoker() is not None
    stale = [] if command in ("doctor", "help") else root_owned_state()  # doctor reports it
    if stale:
        error(f"{len(stale)} path(s) of this environment's state are owned by root, left by an "
              f"earlier run as root (e.g. `sudo bot-bottle install`); give them back with: "
              f"{root_owned_fix(stale)}")
        return 1
    try:
        # A named environment registers on first use; `envs` manages them
        # and must not register the one it removes.
        if command != "envs" and not as_sudo:
            ensure_active_registered()
    except InvalidEnvironmentError as e:
        error(str(e))
        return 2
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
