"""CLI subcommand registry: `bot-bottle <command>` -> `cmd_<name>(argv)`, each
module imported only when its command is dispatched.
"""

from __future__ import annotations

from importlib import import_module
from typing import Callable

# command -> "<submodule>:<handler attr>", so building the registry imports
# nothing.
_HANDLERS: dict[str, str] = {
    "active": "active:cmd_active",  # old name for `bots --running`, kept a release
    "agents": "agents:cmd_agents",
    "attach": "attach:cmd_attach",
    "audit": "audit:cmd_audit",
    "ba": "bottled_agents:cmd_bottled_agents",
    "bootstrap": "bootstrap:cmd_bootstrap",
    "bottles": "bottles:cmd_bottles",
    "bottled-agents": "bottled_agents:cmd_bottled_agents",
    "bots": "bottled_agents:cmd_bottled_agents",
    "cleanup": "cleanup:cmd_cleanup",
    "commit": "commit:cmd_commit",
    "doctor": "doctor:cmd_doctor",
    "down": "lifecycle:cmd_down",
    "envs": "envs:cmd_envs",
    "forge": "forge:cmd_forge",
    "help": "help:cmd_help",
    "host": "host:cmd_host",
    "install": "lifecycle:cmd_install",
    "jobs": "lifecycle:cmd_jobs",  # unlisted; `status --jobs`
    "pair": "pair:cmd_pair",
    "rename": "rename:cmd_rename",
    "resume": "resume:cmd_resume",
    "setup": "setup:cmd_setup",
    "start": "start:cmd_start",
    "status": "lifecycle:cmd_status",
    "supervise": "supervise:cmd_supervise",
    "trust": "trust:cmd_trust",
    "uninstall": "uninstall:cmd_uninstall",
    "up": "lifecycle:cmd_up",
    "version": "version:cmd_version",
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

# Bare `bot-bottle` / `bot-bottle <agent>`: not a command you type by name.
QUICK = _lazy("quick:cmd_quick")
HOME = _lazy("home:cmd_home")

# Commands that never open the store, so they run before or without a migrated
# DB (where the migration prompt can't be answered on a non-TTY runner).
NO_MIGRATION_COMMANDS = frozenset({
    "attach", "audit", "bootstrap", "bottles", "doctor", "down", "envs", "forge", "help", "host",
    "install", "jobs", "pair", "setup", "status", "supervise", "trust", "uninstall", "up",
    "version",
})

__all__ = ["COMMANDS", "HOME", "NO_MIGRATION_COMMANDS", "QUICK"]
