"""Main CLI dispatcher.

Commands: backend, cleanup, commit, edit, info, init, list, resume, start, supervise
"""

from __future__ import annotations

import sys

from ..errors import MissingEnvVarError
from ..log import Die, die, error
from ..manifest import ManifestError
from ..orchestrator.store.store_manager import StoreManager
from ._common import PROG
from .commands import list as _list_mod
from .commands.backend import cmd_backend
from .commands.cleanup import cmd_cleanup
from .commands.commit import cmd_commit
from .commands.edit import cmd_edit
from .commands.info import cmd_info
from .commands.init import cmd_init
from .commands.login import cmd_login
from .commands.resume import cmd_resume
from .commands.start import cmd_start
from .commands.supervise import cmd_supervise

cmd_list = _list_mod.cmd_list

COMMANDS = {
    "backend": cmd_backend,
    "cleanup": cmd_cleanup,
    "commit": cmd_commit,
    "edit": cmd_edit,
    "info": cmd_info,
    "init": cmd_init,
    "list": cmd_list,
    "login": cmd_login,
    "resume": cmd_resume,
    "start": cmd_start,
    "supervise": cmd_supervise,
}

# Commands that manage host prerequisites (or are otherwise store-free) and
# must run before — or without — a migrated DB. `backend` provisions/probes
# the host (TAP pool, /dev/kvm, firecracker) and never opens the store, so
# gating it on the schema breaks preflight on a fresh CI runner where stdin
# isn't a TTY and the migration prompt can't be answered.
NO_MIGRATION_COMMANDS = frozenset({"backend", "login"})


def usage() -> None:
    sys.stderr.write(f"usage: {PROG} <command> [args...]\n\n")
    sys.stderr.write("Commands:\n")
    sys.stderr.write("  backend   set up / check / undo a backend's host prerequisites (setup|status|teardown)\n")
    sys.stderr.write("  cleanup   stop and remove all active bot-bottle containers\n")
    sys.stderr.write("  commit    snapshot a running bottle's container state to a Docker image\n")
    sys.stderr.write("  edit      open an agent in vim for editing\n")
    sys.stderr.write("  info      print env, skills, and prompt details for a named agent\n")
    sys.stderr.write("  init      interactively create a new agent and add it to bot-bottle.json\n")
    sys.stderr.write("  list      list available agents or active containers\n")
    sys.stderr.write("  login     register this host with a bot-bottle console\n")
    sys.stderr.write(
        "  resume    re-launch a bottle by its identity "
        "(continues state from PRD 0016)\n"
    )
    sys.stderr.write(
        "  start     boot a container for a named agent and "
        "attach an interactive session\n"
    )
    sys.stderr.write(
        "  supervise view + approve/modify/reject pending supervise "
        "proposals (PRD 0013)\n\n"
    )
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
