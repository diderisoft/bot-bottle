"""Git provisioning inside a running Docker bottle.

Three concerns, all about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that .git
     into /home/node/workspace/.git so the agent operates on the
     user's repo.
  2. If the bottle declares `git` entries (PRD 0008), write a
     ~/.gitconfig with insteadOf rules so every git operation
     against a declared upstream (push, fetch, clone, pull,
     ls-remote) transparently hits the per-agent git-gate. The
     gate mirrors the upstream in both directions, so URL
     rewriting is symmetric.
  3. If the bottle declares `git.user` (issue #86), set
     `git config --global user.{name,email}` inside the bottle so
     the agent's commits are attributed to that identity.
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
    """Set up git inside the bottle. Runs all three subcases; each
    no-ops when its condition isn't met."""
    _provision_cwd_git(plan, target)
    _provision_git_gate_config(plan, target)
    _provision_git_user(plan, target)


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


def _provision_git_user(plan: DockerBottlePlan, target: str) -> None:
    """Apply `git config --global user.{name,email}` inside the
    bottle so the agent's commits are attributed to the operator-
    chosen identity instead of the agent image's default
    (which is no user — git would refuse to commit at all
    until the agent ran its own `git config`).

    Runs as the `node` user so `--global` lands in
    `/home/node/.gitconfig` (matching the existing
    `_provision_git_gate_config` write location). No-op when the
    bottle didn't declare `git.user`.

    Each field set independently — name-only or email-only
    configs only run the `git config` line for the field
    present."""
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    gu = bottle.git_user
    if gu.is_empty():
        return
    if gu.name:
        info(f"git config --global user.name = {gu.name!r}")
        subprocess.run(
            ["docker", "exec", "-u", "node", target,
             "git", "config", "--global", "user.name", gu.name],
            stdout=subprocess.DEVNULL,
            check=True,
        )
    if gu.email:
        info(f"git config --global user.email = {gu.email!r}")
        subprocess.run(
            ["docker", "exec", "-u", "node", target,
             "git", "config", "--global", "user.email", gu.email],
            stdout=subprocess.DEVNULL,
            check=True,
        )
