"""Main CLI dispatcher.

Commands: backend, cleanup, commit, edit, help, info, init, list, login,
resume, start, supervise
"""

from __future__ import annotations

import sys

from ..errors import MissingEnvVarError
from ..log import Die, die, error
from ..manifest import ManifestError
from ..orchestrator.store.store_manager import StoreManager
from .constants import PROG
from .commands import list as _list_mod
from .commands.backend import cmd_backend
from .commands.cleanup import cmd_cleanup
from .commands.commit import cmd_commit
from .commands.edit import cmd_edit
from .commands.help import cmd_help
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
    "help": cmd_help,
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
NO_MIGRATION_COMMANDS = frozenset({"backend", "help", "login"})


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
