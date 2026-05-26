"""resume: re-launch a bottle by its identity.

Reads ~/.claude-bottle/state/<identity>/metadata.json to recover the
(agent_name, cwd, copy_cwd) the bottle was originally started with,
then runs the same launch core as `start` — but pinned to the
recorded identity so the new bottle picks up any per-bottle Dockerfile
(from capability-block apply) and transcript snapshot under the same
state dir.

Use case: an agent calls capability-block, the dashboard approves
and tears down the bottle, the operator runs
    ./cli.py resume <identity>
to bring up the replacement with the new capabilities baked in.
"""

from __future__ import annotations

import argparse

from ..backend import BottleSpec
from ..backend.docker.bottle_state import read_metadata
from ..log import die
from ..manifest import Manifest
from ._common import PROG, USER_CWD
from .start import _launch_bottle


def cmd_resume(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} resume", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument(
        "identity",
        help="bottle identity from a prior `start` (see its session-end output)",
    )
    args = parser.parse_args(argv)

    metadata = read_metadata(args.identity)
    if metadata is None:
        die(
            f"no state recorded for identity {args.identity!r}; "
            f"check ~/.claude-bottle/state/ or run `cli.py start` to create a new bottle"
        )

    manifest = Manifest.resolve(USER_CWD)
    manifest.require_agent(metadata.agent_name)

    spec = BottleSpec(
        manifest=manifest,
        agent_name=metadata.agent_name,
        copy_cwd=metadata.copy_cwd,
        user_cwd=metadata.cwd or USER_CWD,
        identity=metadata.identity,
    )
    return _launch_bottle(
        spec,
        dry_run=args.dry_run,
        remote_control=args.remote_control,
    )
