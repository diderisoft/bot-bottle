"""Copy the host cwd's .git directory into a running Docker bottle.

Only fires when `--cwd` was passed AND the host cwd actually has a
.git. The container-side path is fixed at /home/node/workspace/.git;
ownership is reset to node so the agent can run git commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ....log import info
from ..bottle_plan import DockerBottlePlan


def provision_git(plan: DockerBottlePlan, target: str) -> None:
    """If --cwd was set and the host cwd has a .git directory, copy
    it into /home/node/workspace/.git and fix ownership. No-op
    otherwise."""
    if not (plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir()):
        return
    container = target
    info(f"copying {plan.spec.user_cwd}/.git -> {container}:/home/node/workspace/.git")
    subprocess.run(
        ["docker", "cp", f"{plan.spec.user_cwd}/.git", f"{container}:/home/node/workspace/.git"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        [
            "docker", "exec", "-u", "0", container,
            "chown", "-R", "node:node", "/home/node/workspace/.git",
        ],
        stdout=subprocess.DEVNULL,
        check=True,
    )
