"""`backend` CLI command — generic host setup/status across backends.

`bot-bottle backend setup    [--backend=NAME]` provisions (or points at how
to provision) the chosen backend's one-time host prerequisites.
`bot-bottle backend status   [--backend=NAME]` reports readiness.
`bot-bottle backend teardown [--backend=NAME]` undoes setup (uninstall).

All dispatch to the backend's `setup()` / `status()` / `teardown()`,
so there are no backend-specific commands — swapping
backends is just a different `--backend` (or `$BOT_BOTTLE_BACKEND`, or
the host default).
"""

from __future__ import annotations

import argparse
import sys

from ...backend import get_bottle_backend, known_backend_names
from ...control.hostctl import service_unit
from ...control.hostctl.sessions import MIN_TMUX_VERSION, tmux_version
from ..constants import PROG
from ..settings import settings_from_env


def _note_tmux() -> None:
    """Agent sessions run in tmux; say so if it's missing or too old."""
    version = tmux_version()
    if version is None or version < MIN_TMUX_VERSION:
        found = "not installed" if version is None else f"{version[0]}.{version[1]}"
        sys.stderr.write(
            f"{PROG}: tmux {MIN_TMUX_VERSION[0]}.{MIN_TMUX_VERSION[1]}+ is required for agent "
            f"sessions ({found}); install it with brew, apt or nix\n")


def _note_service() -> None:
    """Point at the optional service that keeps the host controller running."""
    if not service_unit.installed():
        sys.stderr.write(
            f"{PROG}: tip: `{PROG} install` also sets up autostart, which keeps the host "
            "controller running across reboots\n")


def cmd_backend(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} backend",
        description="Set up or check a backend's host prerequisites.",
    )
    parser.add_argument(
        "action",
        choices=("setup", "status", "teardown"),
        help="setup: provision/print host prerequisites; status: report "
             "readiness; teardown: undo setup (uninstall)",
    )
    parser.add_argument(
        "--backend",
        choices=known_backend_names(),
        default=None,
        help="backend to target (default: $BOT_BOTTLE_BACKEND or the host default)",
    )
    ns = parser.parse_args(args)

    backend = get_bottle_backend(settings_from_env(), ns.backend)
    if ns.action == "setup":
        _note_tmux()
        _note_service()
        return backend.setup()
    if ns.action == "teardown":
        return backend.teardown()
    return backend.status()
