"""cleanup: stop and remove all orphaned bot-bottle resources.

First the host controller tears down the bottles it holds (their agents are
stopped), then each backend reaps whatever is left.

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
from ...contract.hostctl import BOTTLES_PATH
from ...control.hostctl import runner
from ...control.hostctl.client import HostControlClient
from ...lib import log
from ...lib.log import info
from ...util import read_tty_line
from ..settings import settings_from_env
from .active import held_bottles


def cmd_cleanup(_argv: list[str]) -> int:
    # Order: stable backend iteration so the y/N output is
    # deterministic across runs. Skip backends whose runtime
    # isn't available on this host so e.g. macos-container
    # doesn't error on Linux.
    settings = settings_from_env()
    plans = [
        (name, get_bottle_backend(settings, name))
        for name in known_backend_names()
        if has_backend(settings, name)
    ]
    prepared = [(name, b, b.prepare_cleanup()) for name, b in plans]
    held = sorted(held_bottles())

    if all(p.empty for _, _, p in prepared) and not held:
        info("no bot-bottle resources to clean up")
        return 0

    if held:
        info("--- host controller (agents are stopped) ---")
        for bottle_id in held:
            info(f"  bottle {bottle_id}")
    for name, _, plan in prepared:
        if plan.empty:
            continue
        info(f"--- {name} backend ---")
        plan.print()

    if not _prompt_yes("remove all of the above?"):
        info("cleanup: skipped")
        return 0

    failures = _teardown_held(held)
    # Confirmation authorizes a fresh authoritative snapshot, not blind use of
    # identities that may have changed while the operator reviewed the preview.
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


def _teardown_held(bottle_ids: list[str]) -> list[str]:
    """Ask the host controller to tear down each of `bottle_ids`, streaming
    its output; one failure message per bottle that didn't go."""
    if not bottle_ids:
        return []
    state = runner.running()
    if state is None:
        return [f"host controller: not running; can't stop {', '.join(bottle_ids)}"]
    client = HostControlClient(state, runner.cli_token())
    failures: list[str] = []
    for bottle_id in bottle_ids:
        try:
            job = str(client.delete(f"{BOTTLES_PATH}/{bottle_id}")["job"])
            final = client.follow(job, log.write_line)
        except runner.HostControlError as e:
            failures.append(f"{bottle_id}: {e}")
            continue
        if final is None:
            failures.append(f"{bottle_id}: detached; its teardown continues as job {job}")
        elif final.get("type") == "error":
            failures.append(f"{bottle_id}: {final.get('message')}")
    return failures


def _prompt_yes(message: str) -> bool:
    sys.stderr.write(f"bot-bottle: {message} [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")
