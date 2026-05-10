"""Main CLI dispatcher.

Commands: build, cleanup, edit, info, init, list, start
"""

from __future__ import annotations

import sys

from ..log import Die, die
from ._common import PROG
from .build import cmd_build
from .cleanup import cmd_cleanup
from .edit import cmd_edit
from .info import cmd_info
from .init import cmd_init
from .list import cmd_list
from .start import cmd_start

COMMANDS = {
    "build": cmd_build,
    "cleanup": cmd_cleanup,
    "edit": cmd_edit,
    "info": cmd_info,
    "init": cmd_init,
    "list": cmd_list,
    "start": cmd_start,
}


def usage() -> None:
    sys.stderr.write(f"usage: {PROG} <command> [args...]\n\n")
    sys.stderr.write("Commands:\n")
    sys.stderr.write("  build     build (or rebuild) the claude-bottle Docker image\n")
    sys.stderr.write("  cleanup   stop and remove all active claude-bottle containers\n")
    sys.stderr.write("  edit      open an agent in vim for editing\n")
    sys.stderr.write("  info      print env, skills, and prompt details for a named agent\n")
    sys.stderr.write("  init      interactively create a new agent and add it to claude-bottle.json\n")
    sys.stderr.write("  list      list available agents or active containers\n")
    sys.stderr.write("  start     boot a container for a named agent and attach an interactive session\n\n")
    sys.stderr.write(f"Run '{PROG} <command> --help' for command-specific usage.\n")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        usage()
        return 2
    command = argv[0]
    rest = argv[1:]
    if command in ("-h", "--help"):
        usage()
        return 0
    handler = COMMANDS.get(command)
    if handler is None:
        usage()
        die(f"unknown command: {command}")
    try:
        return handler(rest) or 0
    except Die as e:
        return e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
