"""CLI subcommand registry.

One module per `bot-bottle <command>`, each exposing a `cmd_<name>(argv)`
handler. This package `__init__` maps command names to their handlers
**lazily**: a short-lived CLI run dispatches exactly one command, so
importing all twelve handlers (and their transitive deps — backend,
manifest, orchestrator, …) up front is wasted work. Each COMMANDS value is
a thin wrapper that imports its handler's module on first call. Shared CLI
helpers (`constants`, `tui`) stay one level up in the `cli` package.
"""

from __future__ import annotations

from importlib import import_module
from typing import Callable

# command name -> "<submodule>:<handler attr>". Kept as strings so building
# the registry imports nothing; the module loads only when dispatched.
_HANDLERS: dict[str, str] = {
    "active": "active:cmd_active",
    "attach": "attach:cmd_attach",
    "audit": "audit:cmd_audit",
    "backend": "backend:cmd_backend",
    "cleanup": "cleanup:cmd_cleanup",
    "commit": "commit:cmd_commit",
    "down": "lifecycle:cmd_down",
    "doctor": "doctor:cmd_doctor",
    "help": "help:cmd_help",
    "install": "lifecycle:cmd_install",
    "jobs": "lifecycle:cmd_jobs",
    "list": "list:cmd_list",
    "login": "login:cmd_login",
    "pair": "pair:cmd_pair",
    "remote": "remote:cmd_remote",
    "resume": "resume:cmd_resume",
    "setup": "setup:cmd_setup",
    "start": "start:cmd_start",
    "status": "lifecycle:cmd_status",
    "supervise": "supervise:cmd_supervise",
    "uninstall": "lifecycle:cmd_uninstall",
    "up": "lifecycle:cmd_up",
}


def _lazy(spec: str) -> Callable[[list[str]], "int | None"]:
    """Wrap a `<module>:<attr>` handler so its module is imported only when
    the command is actually dispatched, not when the registry is built."""
    module, attr = spec.split(":")

    def run(argv: list[str]) -> "int | None":
        handler = getattr(import_module(f".{module}", __name__), attr)
        return handler(argv)

    run.__name__ = attr
    return run


COMMANDS = {name: _lazy(spec) for name, spec in _HANDLERS.items()}

# Commands that manage host prerequisites (or are otherwise store-free) and
# must run before — or without — a migrated DB. `backend` provisions/probes
# the host (TAP pool, /dev/kvm, firecracker) and never opens the store, so
# gating it on the schema breaks preflight on a fresh CI runner where stdin
# isn't a TTY and the migration prompt can't be answered. `help` and `login`
# likewise never touch the store, nor do the host-service commands.
NO_MIGRATION_COMMANDS = frozenset({
    "attach", "audit", "backend", "doctor", "down", "help", "install", "jobs", "login",
    "pair", "remote", "setup", "status", "uninstall", "up",
})

__all__ = ["COMMANDS", "NO_MIGRATION_COMMANDS"]
