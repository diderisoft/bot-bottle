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

from ..bottles import get_bottle_platform
from ..bottles.docker import DockerBottlePlan, DockerBottleSpec
from ..log import info
from ..manifest import Manifest
from ._common import PROG, USER_CWD, read_tty_line


def show_plan(plan: DockerBottlePlan, *, remote_control: bool) -> None:
    """Render the y/N preflight summary to stderr. Reads everything off
    the plan; pure presentation."""
    spec = plan.spec
    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)

    env_names = list(bottle.env.keys())
    if spec.forward_oauth_token:
        env_names.append("CLAUDE_CODE_OAUTH_TOKEN")

    ssh_hosts = [e.Host for e in bottle.ssh]
    prompt_first_line = agent.prompt.splitlines()[0] if agent.prompt else ""
    runtime_label = "runsc (gVisor)" if plan.use_runsc else "runc (default)"

    print(file=sys.stderr)
    info(f"agent           : {spec.agent_name}")
    info(f"image           : {plan.image}")
    if plan.derived_image:
        info(
            f"cwd             : {spec.user_cwd} -> /home/node/workspace "
            f"(derived: {plan.derived_image})"
        )
    info(f"container       : {plan.container_name}")
    info(f"stage dir       : {plan.stage_dir}")
    info("env (names only): " + (", ".join(env_names) if env_names else "(none)"))
    info("skills          : " + (" ".join(agent.skills) if agent.skills else "(none)"))
    info(f"docker runtime  : {runtime_label}")
    info(f"bottle             : {agent.bottle}")
    if ssh_hosts:
        info(f"  ssh hosts     : {', '.join(ssh_hosts)}")
    else:
        info("  ssh hosts     : (none)")
    info(f"  egress        : {plan.allowlist_summary}")
    info(
        f"prompt          : {len(agent.prompt)} chars; "
        f"first line: {prompt_first_line or '(empty)'}"
    )
    info("remote-control  : " + ("enabled" if remote_control else "disabled"))
    print(file=sys.stderr)


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into a derived image")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("CLAUDE_BOTTLE_DRY_RUN") == "1"

    manifest = Manifest.resolve(USER_CWD)
    spec = DockerBottleSpec(
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
        show_plan(plan, remote_control=args.remote_control)

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
