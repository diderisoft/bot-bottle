"""Git provisioning inside a running Docker bottle.

Two concerns, both about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that .git
     into /home/node/workspace/.git so the agent operates on the
     user's repo.
  2. If the bottle declares `git` entries (PRD 0008), write a
     ~/.gitconfig with insteadOf rules so every git operation
     against a declared upstream (push, fetch, clone, pull,
     ls-remote) transparently hits the per-agent git-gate. The
     gate mirrors the upstream in both directions, so URL
     rewriting is symmetric.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ....git_gate import GIT_GATE_HOSTNAME, git_gate_render_gitconfig
from ....log import info
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan


def provision_git(plan: DockerBottlePlan, target: str) -> None:
    """Set up git inside the bottle. Runs both subcases; each no-ops
    when its condition isn't met."""
    _provision_cwd_git(plan, target)
    _provision_git_gate_config(plan, target)


def _provision_cwd_git(plan: DockerBottlePlan, target: str) -> None:
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


def _provision_git_gate_config(plan: DockerBottlePlan, target: str) -> None:
    """Write ~/.gitconfig in the bottle with the git-gate
    insteadOf rules. No-op when the bottle has no `git` entries."""
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not bottle.git:
        return
    container = target
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_gitconfig = f"{container_home}/.gitconfig"

    content = git_gate_render_gitconfig(bottle.git, GIT_GATE_HOSTNAME)
    config_file = plan.stage_dir / "agent_gitconfig"
    config_file.write_text(content)
    config_file.chmod(0o600)

    info(f"writing {container_gitconfig} with {len(bottle.git)} insteadOf rule(s)")
    subprocess.run(
        ["docker", "cp", str(config_file), f"{container}:{container_gitconfig}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(container, ["chown", "node:node", container_gitconfig])
    docker_mod.docker_exec_root(container, ["chmod", "644", container_gitconfig])
