"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..platform import BottleSpec, get_bottle_platform
from ..log import info
from ..manifest import Manifest
from ._common import PROG, USER_CWD, read_tty_line


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into a derived image")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("CLAUDE_BOTTLE_DRY_RUN") == "1"

    manifest = Manifest.resolve(USER_CWD)
    spec = BottleSpec(
        manifest=manifest,
        agent_name=args.name,
        copy_cwd=args.cwd,
        user_cwd=USER_CWD,
        forward_oauth_token=bool(os.environ.get("CLAUDE_BOTTLE_OAUTH_TOKEN")),
    )

    stage_dir = Path(tempfile.mkdtemp(prefix="claude-bottle-stage."))
    try:
        platform = get_bottle_platform()
        plan = platform.prepare(spec, stage_dir=stage_dir)
        plan.print(remote_control=args.remote_control)

        if dry_run:
            info("dry-run requested; not starting container.")
            return 0

        sys.stderr.write("claude-bottle: launch this agent? [y/N] ")
        sys.stderr.flush()
        reply = read_tty_line()
        if reply not in ("y", "Y", "yes", "YES"):
            info("aborted by user")
            return 0

        with platform.launch(plan) as bottle:
            info(
                "attaching interactive claude session "
                "(Ctrl-D or 'exit' to leave; container will be removed)"
            )
            claude_args = ["--dangerously-skip-permissions"]
            if args.remote_control:
                claude_args.append("--remote-control")
            bottle.exec_claude(claude_args, tty=True)
            info(f"session ended; container {bottle.name} will be removed")
            return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
