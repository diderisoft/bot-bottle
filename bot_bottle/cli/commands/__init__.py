"""CLI subcommand registry.

One module per `bot-bottle <command>`, each exposing a `cmd_<name>(argv)`
handler. This package `__init__` assembles them into the COMMANDS table
the dispatcher (`bot_bottle.cli.__main__`) reads. Shared CLI helpers
(`constants`, `tui`) stay one level up in the `cli` package.
"""

from __future__ import annotations

from .backend import cmd_backend
from .cleanup import cmd_cleanup
from .commit import cmd_commit
from .edit import cmd_edit
from .help import cmd_help
from .info import cmd_info
from .init import cmd_init
from .list import cmd_list
from .login import cmd_login
from .resume import cmd_resume
from .start import cmd_start
from .supervise import cmd_supervise

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
# isn't a TTY and the migration prompt can't be answered. `help` and `login`
# likewise never touch the store.
NO_MIGRATION_COMMANDS = frozenset({"backend", "help", "login"})

__all__ = ["COMMANDS", "NO_MIGRATION_COMMANDS"]
