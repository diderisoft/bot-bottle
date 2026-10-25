"""CLI subcommand registry: `bot-bottle <command>` -> `cmd_<name>(argv)`, each
module imported only when its command is dispatched.
"""

from __future__ import annotations

from importlib import import_module
from typing import Callable

# command -> "<submodule>:<handler attr>", so building the registry imports
# nothing.
_HANDLERS: dict[str, str] = {
    "active": "active:cmd_active",
    "approve": "approve:cmd_approve",
    "attach": "attach:cmd_attach",
    "audit": "audit:cmd_audit",
    "backend": "backend:cmd_backend",
    "cleanup": "cleanup:cmd_cleanup",
    "commit": "commit:cmd_commit",
    "down": "lifecycle:cmd_down",
    "default": "quick:cmd_default",
    "doctor": "doctor:cmd_doctor",
    "help": "help:cmd_help",
    "install": "lifecycle:cmd_install",
    "jobs": "lifecycle:cmd_jobs",
    "list": "list:cmd_list",
    "login": "login:cmd_login",
    "pair": "pair:cmd_pair",
    "quick": "quick:cmd_quick",
    "remote": "remote:cmd_remote",
    "resume": "resume:cmd_resume",
    "setup": "setup:cmd_setup",
    "start": "start:cmd_start",
    "status": "lifecycle:cmd_status",
    "supervise": "supervise:cmd_supervise",
    "trust": "trust:cmd_trust",
    "uninstall": "uninstall:cmd_uninstall",
    "up": "lifecycle:cmd_up",
}


def _lazy(spec: str) -> Callable[[list[str]], "int | None"]:
    """Wrap a `<module>:<attr>` handler to import its module on first call."""
    module, attr = spec.split(":")

    def run(argv: list[str]) -> "int | None":
        handler = getattr(import_module(f".{module}", __name__), attr)
        return handler(argv)

    run.__name__ = attr
    return run


COMMANDS = {name: _lazy(spec) for name, spec in _HANDLERS.items()}

# Commands that never open the store, so they run before or without a migrated
# DB (where the migration prompt can't be answered on a non-TTY runner).
NO_MIGRATION_COMMANDS = frozenset({
    "approve", "attach", "audit", "backend", "doctor", "down", "help", "install", "jobs", "login",
    "pair", "remote", "setup", "trust", "status", "uninstall", "up",
})

__all__ = ["COMMANDS", "NO_MIGRATION_COMMANDS"]
