"""`backend` CLI command — generic host setup/status across backends.

`./cli.py backend setup    [--backend=NAME]` provisions (or points at how
to provision) the chosen backend's one-time host prerequisites.
`./cli.py backend status   [--backend=NAME]` reports readiness.
`./cli.py backend teardown [--backend=NAME]` undoes setup (uninstall).

All dispatch to the backend's `setup()` / `status()` / `teardown()`
classmethods, so there are no backend-specific commands — swapping
backends is just a different `--backend` (or `$BOT_BOTTLE_BACKEND`, or
the host default).
"""

from __future__ import annotations

import argparse

from ...backend import get_bottle_backend, known_backend_names
from ..constants import PROG


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

    backend = get_bottle_backend(ns.backend)
    if ns.action == "setup":
        return backend.setup()
    if ns.action == "teardown":
        return backend.teardown()
    return backend.status()
