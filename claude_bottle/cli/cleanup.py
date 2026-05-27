"""cleanup: stop and remove all orphaned claude-bottle resources.

Walks every registered backend (docker + smolmachines) so a single
`./cli.py cleanup` reaps both backends' leftovers — orphaned
smolvm machines won't survive a docker-only cleanup pass (issue
addressed alongside #77).

Each backend's `prepare_cleanup` enumerates its own resources;
docker's `_list_orphan_state_dirs` consults
`enumerate_active_agents()` for the union of live identities so
state dirs of running smolmachines bottles aren't reaped. State
dirs are shared layout, so docker is the single owner of that
bucket.

State dirs with `.preserve` are intentionally never touched — they
hold capability-block rebuilds or crash snapshots the operator may
want to `resume`. Manual `rm -rf ~/.claude-bottle/state/<identity>`
is the path for those.
"""

from __future__ import annotations

import sys

from ..backend import get_bottle_backend, known_backend_names
from ..log import info
from ._common import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    # Order: stable backend iteration so the y/N output is
    # deterministic across runs.
    plans = [
        (name, get_bottle_backend(name)) for name in known_backend_names()
    ]
    prepared = [(name, b, b.prepare_cleanup()) for name, b in plans]

    if all(p.empty for _, _, p in prepared):
        info("no claude-bottle resources to clean up")
        return 0

    for name, _, plan in prepared:
        if plan.empty:
            continue
        info(f"--- {name} backend ---")
        plan.print()

    if not _prompt_yes("remove all of the above?"):
        info("cleanup: skipped")
        return 0

    for name, backend, plan in prepared:
        if plan.empty:
            continue
        backend.cleanup(plan)
    info("cleanup: done")
    return 0


def _prompt_yes(message: str) -> bool:
    sys.stderr.write(f"claude-bottle: {message} [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")
