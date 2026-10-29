"""cleanup: remove what bot-bottle left behind.

  bot-bottle cleanup bots [--backend NAME]
  bot-bottle cleanup images [--dry-run] [--older-than DAYS] [--build-store]

`bots` stops and removes leftover bottled agents: the host controller tears
down the bottled agents it holds, then each installed backend (or just NAME)
reaps what's left, then orphaned state dirs go. A backend that can't be listed
is reported and skipped, and its state dirs are left alone. Dirs with
`.preserve` are never touched.

`images` frees disk: see `bot-bottle cleanup images --help`.
"""

from __future__ import annotations

import sys
from typing import Any

from ...backend import EnumerationError, get_bottle_backend, has_backend, known_backend_names
from ...backend.cleanup_control import CleanupError
from ...backend.state_cleanup import cleanup_state, prepare_state_cleanup
from ...contract.hostctl import BOTTLES_PATH
from ...control.hostctl import runner
from ...control.hostctl.client import HostControlClient
from ...lib import log
from ...lib.log import info
from ...util import read_tty_line
from ..constants import PROG
from ..settings import settings_from_env
from .active import held_bottles


def cmd_cleanup(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0 if argv else 2
    if argv[0] == "images":
        # pylint: disable-next=import-outside-toplevel
        from .cleanup_images import cmd_cleanup_images
        return cmd_cleanup_images(argv[1:])
    if argv[0] != "bots":
        sys.stderr.write(f"{PROG} cleanup: unknown target {argv[0]!r}\n\n{__doc__}")
        return 2
    return _cleanup_bots(argv[1:])


def _cleanup_bots(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        sys.stderr.write(__doc__ or "")
        return 0
    only = _backend_arg(argv)
    settings = settings_from_env()
    # List every available backend's live bottles, since state dirs are shared;
    # sweep only the selected ones.
    live: set[str] = set()
    listed: set[str] = set()
    prepared: list[tuple[str, Any, Any]] = []
    for name in known_backend_names():
        if not has_backend(settings, name):
            if name == only:
                info(f"{name}: not available on this host")
            continue
        backend = get_bottle_backend(settings, name)
        try:
            live.update(agent.slug for agent in backend.enumerate_active())
            plan = backend.prepare_cleanup() if only in (None, name) else None
        except EnumerationError as e:
            info(f"{name}: skipped, couldn't list its resources ({e})")
            continue
        listed.add(name)
        if plan is not None:
            prepared.append((name, backend, plan))
    swept = listed if only is None else listed & {only}
    state = prepare_state_cleanup(live, swept)
    held = sorted(held_bottles())

    if all(p.empty for _, _, p in prepared) and state.empty and not held:
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
    if not state.empty:
        info("--- state dirs ---")
        state.print()

    if not _prompt_yes("remove all of the above?"):
        info("cleanup: skipped")
        return 0

    failures = _teardown_held(held)
    # Confirmation re-takes a fresh snapshot rather than trust the preview.
    for name, backend, displayed in prepared:
        try:
            approved = displayed.intersect(backend.prepare_cleanup())
            if not approved.empty:
                backend.cleanup(approved)
        except (CleanupError, EnumerationError) as exc:
            failures.append(f"{name}: {exc}")
    try:
        current = prepare_state_cleanup(live, swept)
        approved_state = state.intersect(current)
        if not approved_state.empty:
            cleanup_state(approved_state, settings)
    except CleanupError as exc:
        failures.append(f"state dirs: {exc}")
    if failures:
        raise CleanupError("cleanup incomplete: " + "; ".join(failures))
    info("cleanup: done")
    return 0


def _backend_arg(argv: list[str]) -> str | None:
    """`--backend NAME` (or `--backend=NAME`), validated; None for all."""
    for i, arg in enumerate(argv):
        name = None
        if arg == "--backend":
            name = argv[i + 1] if i + 1 < len(argv) else ""
        elif arg.startswith("--backend="):
            name = arg.split("=", 1)[1]
        if name is not None:
            if name not in known_backend_names():
                raise SystemExit(f"bot-bottle cleanup bots: unknown backend {name!r}; "
                                 f"one of {', '.join(known_backend_names())}")
            return name
    return None


def _teardown_held(bottle_ids: list[str]) -> list[str]:
    """Ask the host controller to tear down each of `bottle_ids`, streaming its
    output; one failure message per bottle that didn't go."""
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
