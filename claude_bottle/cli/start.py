"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends.

The launch core is shared with `cli.py resume <identity>`: see
_launch_bottle below.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..backend import BottleSpec, get_bottle_backend
from ..log import die, info
from ..manifest import Manifest
from ._common import PROG, USER_CWD, read_tty_line


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into a derived image")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="preflight output format; --format=json requires --dry-run",
    )
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("CLAUDE_BOTTLE_DRY_RUN") == "1"
    if args.format == "json" and not dry_run:
        die("--format=json requires --dry-run")

    manifest = Manifest.resolve(USER_CWD)
    spec = BottleSpec(
        manifest=manifest,
        agent_name=args.name,
        copy_cwd=args.cwd,
        user_cwd=USER_CWD,
    )
    return _launch_bottle(
        spec,
        dry_run=dry_run,
        output_format=args.format,
        remote_control=args.remote_control,
    )


def _launch_bottle(
    spec: BottleSpec,
    *,
    dry_run: bool,
    output_format: str,
    remote_control: bool,
) -> int:
    """Shared launch core for `start` and `resume`. Builds the plan,
    prints / dry-runs / prompts as appropriate, brings the bottle up,
    attaches claude, and prints the resume hint on session end."""
    stage_dir = Path(tempfile.mkdtemp(prefix="claude-bottle-stage."))
    try:
        backend = get_bottle_backend()
        plan = backend.prepare(spec, stage_dir=stage_dir)

        if output_format == "json":
            json.dump(plan.to_dict(remote_control=remote_control), sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0

        plan.print(remote_control=remote_control)

        if dry_run:
            info("dry-run requested; not starting container.")
            return 0

        sys.stderr.write("claude-bottle: launch this agent? [y/N] ")
        sys.stderr.flush()
        reply = read_tty_line()
        if reply not in ("y", "Y", "yes", "YES"):
            info("aborted by user")
            return 0

        identity = _identity_from_plan(plan)
        with backend.launch(plan) as bottle:
            info(
                "attaching interactive claude session "
                "(Ctrl-D or 'exit' to leave; container will be removed)"
            )
            claude_args = ["--dangerously-skip-permissions"]
            if remote_control:
                claude_args.append("--remote-control")
            bottle.exec_claude(claude_args, tty=True)
            info(f"session ended; container {bottle.name} will be removed")
            if identity:
                info(f"to resume this bottle: ./cli.py resume {identity}")
            return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


def _identity_from_plan(plan: object) -> str:
    """Backend-specific: the docker plan exposes the identity as
    `.slug`. Other backends in the future would expose their own
    identity attribute; for now we duck-type to keep this layer
    backend-agnostic."""
    return getattr(plan, "slug", "")
