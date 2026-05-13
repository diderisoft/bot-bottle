"""Git provisioning inside a running Docker bottle.

Two concerns, both about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that .git
     into /home/node/workspace/.git so the agent operates on the
     user's repo.
  2. If the bottle declares `git` entries (PRD 0008), write a
     ~/.gitconfig with pushInsteadOf rules so a `git push <upstream>`
     from inside the agent transparently hits the per-agent git-gate
     instead of the real remote. Fetch keeps the original URL — v1
     gates push only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ....log import info
from ....manifest import GitEntry
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan
from ..git_gate import git_gate_host


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


def render_git_gate_gitconfig(slug: str, entries: tuple[GitEntry, ...]) -> str:
    """Render the ~/.gitconfig content for git-gate `pushInsteadOf`
    rewrites. Pure host-side, no docker; exposed for tests.

    Empty `entries` returns an empty string so callers can no-op
    cleanly without conditional formatting at the call site."""
    if not entries:
        return ""
    gate = git_gate_host(slug)
    out = [
        "# claude-bottle git-gate (PRD 0008): pushes to declared upstreams\n",
        "# transparently route through the gitleaks-scanning git-gate.\n",
        "# Fetch keeps the original URL (v1 gates push only).\n",
    ]
    for entry in entries:
        out.append(f'[url "git://{gate}/{entry.Name}.git"]\n')
        out.append(f"\tpushInsteadOf = {entry.Upstream}\n")
    return "".join(out)


def _provision_git_gate_config(plan: DockerBottlePlan, target: str) -> None:
    """Write ~/.gitconfig in the bottle with the git-gate
    pushInsteadOf rules. No-op when the bottle has no `git` entries."""
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not bottle.git:
        return
    container = target
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_gitconfig = f"{container_home}/.gitconfig"

    content = render_git_gate_gitconfig(plan.slug, bottle.git)
    config_file = plan.stage_dir / "agent_gitconfig"
    config_file.write_text(content)
    config_file.chmod(0o600)

    info(f"writing {container_gitconfig} with {len(bottle.git)} pushInsteadOf rule(s)")
    subprocess.run(
        ["docker", "cp", str(config_file), f"{container}:{container_gitconfig}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(container, ["chown", "node:node", container_gitconfig])
    docker_mod.docker_exec_root(container, ["chmod", "644", container_gitconfig])
