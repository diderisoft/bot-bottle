"""resume: relaunch a bottle by identity from its recorded metadata (agent,
cwd, copy mode, bottles, backend), through the host controller, under the same
identity and state dir.

    bot-bottle resume <identity>
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace

from ...bottle_state import read_metadata
from ...contract.hostctl import LaunchRequest
from ...lib.log import die
from ...manifest import ManifestIndex
from ..constants import PROG
from ..settings import settings_from_env
from .start import _launch_bottle


def cmd_resume(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} resume", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "identity",
        help=f"bottle identity from a prior `start` (`{PROG} stopped` lists them)",
    )
    args = parser.parse_args(argv)

    metadata = read_metadata(args.identity)
    if metadata is None:
        die(
            f"no state recorded for identity {args.identity!r}; "
            f"`{PROG} stopped` lists the ones there are, or run `{PROG} start` "
            f"to create a new bottle"
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
    )
    return _launch_bottle(request, manifest, dry_run=args.dry_run)
