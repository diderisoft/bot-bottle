"""cleanup: stop and remove all orphaned bot-bottle resources.

Walks every registered backend (docker, firecracker, macos-container)
so a single `bot-bottle cleanup` reaps every backend's leftovers — a
firecracker bottle's VM processes and run dirs won't survive a
docker-only cleanup pass (issue addressed alongside #77).

Each backend's `prepare_cleanup` enumerates its own resources;
docker's `_list_orphan_state_dirs` consults
`enumerate_active_agents()` for the union of live identities so
state dirs of running non-docker bottles aren't reaped. State
dirs are shared layout, so docker is the single owner of that
bucket.

State dirs with `.preserve` are intentionally never touched — they
hold preserved sessions the operator may want to `resume`. Manual
`rm -rf ~/.bot-bottle/state/<identity>` is the path for those.
"""

from __future__ import annotations

import sys

from ...backend import get_bottle_backend, has_backend, known_backend_names
from ...backend.cleanup_control import CleanupError
from ...lib.log import info
from ...util import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    # Order: stable backend iteration so the y/N output is
    # deterministic across runs. Skip backends whose runtime
    # isn't available on this host so e.g. macos-container
    # doesn't error on Linux.
    plans = [
        (name, get_bottle_backend(name))
        for name in known_backend_names()
        if has_backend(name)
    ]
    prepared = [(name, b, b.prepare_cleanup()) for name, b in plans]

    if all(p.empty for _, _, p in prepared):
        info("no bot-bottle resources to clean up")
        return 0

    for name, _, plan in prepared:
        if plan.empty:
            continue
        info(f"--- {name} backend ---")
        plan.print()

    if not _prompt_yes("remove all of the above?"):
        info("cleanup: skipped")
        return 0

    # Confirmation authorizes a fresh authoritative snapshot, not blind use of
    # identities that may have changed while the operator reviewed the preview.
    failures: list[str] = []
    for name, backend, displayed in prepared:
        current = backend.prepare_cleanup()
        approved = displayed.intersect(current)
        if approved.empty:
            continue
        try:
            backend.cleanup(approved)
        except CleanupError as exc:
            failures.append(f"{name}: {exc}")
    if failures:
        raise CleanupError("cleanup incomplete: " + "; ".join(failures))
    info("cleanup: done")
    return 0


def _prompt_yes(message: str) -> bool:
    sys.stderr.write(f"bot-bottle: {message} [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")
