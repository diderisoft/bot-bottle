"""resume: re-launch a bottle by its identity.

Reads ~/.bot-bottle/state/<identity>/metadata.json to recover the
(agent_name, cwd, copy_cwd, bottles, backend) the bottle was originally
started with, then launches through the host controller like `start`, pinned
to the recorded identity so the new bottle picks up any per-bottle Dockerfile
override and transcript snapshot under the same state dir.

Use case: an interrupted or preserved bottle needs to be relaunched;
the operator runs
    bot-bottle resume <identity>
to bring up the replacement from the recorded state.
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
        help="bottle identity from a prior `start` (see its session-end output)",
    )
    args = parser.parse_args(argv)

    metadata = read_metadata(args.identity)
    if metadata is None:
        die(
            f"no state recorded for identity {args.identity!r}; "
            f"check ~/.bot-bottle/state/ or run `cli.py start` to create a new bottle"
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
        mount_cwd=metadata.mount_cwd,
    )
    return _launch_bottle(request, manifest, dry_run=args.dry_run)
