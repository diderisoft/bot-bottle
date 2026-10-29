"""resume: start a stopped bottled agent again from its recorded metadata
(agent, cwd, copy mode, bottles, backend), through the host controller, under
the same identity and state dir. With none named, pick one from
`bots --stopped`'s table: arrows move, typing filters, Enter resumes.

    bot-bottle resume [<bottled agent>]
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

from ...bottle_state import read_metadata
from ...contract.hostctl import LaunchRequest
from ...lib.log import die, info
from ...manifest import ManifestIndex
from ..constants import PROG
from ..settings import settings_from_env
from .start import _launch_bottle


def pick_stopped() -> str | None:
    """Pick a stopped bottled agent from `bots --stopped`'s table: arrows to
    move, typing filters, Enter resumes. None when there's none, no terminal,
    or the pick is cancelled."""
    # pylint: disable=import-outside-toplevel
    from .. import tui
    from ..table import format_table
    from .bottled_agents import stopped_table
    header, rows = stopped_table()
    if not rows:
        info(f"no stopped bottled agents to resume (`{PROG} bots` lists them all)")
        return None
    if not sys.stdin.isatty():
        die(f"name the bottled agent to resume: `{PROG} resume <bottled agent>` "
            f"(`{PROG} bots --stopped` lists them)")
    lines = format_table(header, rows, drop_empty=True)
    choice = tui.filter_select(lines[1:], title="Resume a bottled agent", header=lines[0],
                               typing_filters=True)
    return rows[lines.index(choice) - 1][0] if choice is not None else None


def cmd_resume(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} resume", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "identity", metavar="bottled-agent", nargs="?",
        help=f"the bottled agent to resume; omit to pick one (`{PROG} bots --stopped` "
             "lists them)",
    )
    args = parser.parse_args(argv)
    if args.identity is None:
        picked = pick_stopped()
        if picked is None:
            return 1
        args.identity = picked

    metadata = read_metadata(args.identity)
    if metadata is None:
        die(
            f"no bottled agent {args.identity!r} to resume; "
            f"`{PROG} bots --stopped` lists the ones there are, or run `{PROG} start` "
            f"to bottle an agent"
        )

    # The host controller resolves the manifest from the same directory.
    cwd = metadata.cwd or os.getcwd()
    manifest = ManifestIndex.resolve(cwd)
    manifest.require_agent(metadata.agent_name)

    settings = settings_from_env()
    if metadata.backend:
        settings = replace(
            settings, operation=replace(settings.operation, backend=metadata.backend))
    request = LaunchRequest(
        agent=metadata.agent_name,
        bottles=tuple(metadata.bottle_names),
        identity=metadata.identity,
        settings=settings,
        cwd=cwd,
        copy_cwd=metadata.copy_cwd,
        # Picking or naming it is the yes, so a stale image only warns.
        assume_yes=True,
    )
    return _launch_bottle(request, manifest, dry_run=args.dry_run)
