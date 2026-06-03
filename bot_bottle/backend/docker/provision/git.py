"""Git provisioning inside a running Docker bottle.

Three concerns, all about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that .git
     into the planned guest workspace so the agent operates on the
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
import shlex

from ....git_gate import GIT_GATE_HOSTNAME, git_gate_render_gitconfig
from ....log import info
from ... import Bottle
from ..bottle_plan import DockerBottlePlan


def provision_git(plan: DockerBottlePlan, bottle: Bottle) -> None:
    """Set up git inside the bottle. Runs all three subcases; each
    no-ops when its condition isn't met."""
    _provision_cwd_git(plan, bottle)
    _provision_git_gate_config(plan, bottle)
    _provision_git_user(plan, bottle)


def _provision_cwd_git(plan: DockerBottlePlan, bottle: Bottle) -> None:
    """If --cwd was set and the host cwd has a .git directory, copy
    it into /home/node/workspace/.git and fix ownership. No-op
    otherwise."""
    workspace = plan.workspace_plan
    if not (workspace.enabled and workspace.copy_git and workspace.has_host_git_dir):
        return
    guest_workspace_git = f"{workspace.guest_path}/.git"
    host_git = str(workspace.host_path / ".git")
    info(f"copying {host_git} -> {bottle.name}:{guest_workspace_git}")
    bottle.cp_in(host_git, guest_workspace_git)
    bottle.exec(
        f"chown -R {shlex.quote(workspace.owner)} {shlex.quote(guest_workspace_git)}",
        user="root",
    )


def _provision_git_gate_config(plan: DockerBottlePlan, bottle: Bottle) -> None:
    """Write ~/.gitconfig in the bottle with the git-gate
    insteadOf rules. No-op when the bottle has no `git` entries."""
    manifest_bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not manifest_bottle.git:
        return
    container_home = os.environ.get("BOT_BOTTLE_CONTAINER_HOME", "/home/node")
    container_gitconfig = f"{container_home}/.gitconfig"

    content = git_gate_render_gitconfig(manifest_bottle.git, GIT_GATE_HOSTNAME)
    config_file = plan.stage_dir / "agent_gitconfig"
    config_file.write_text(content)
    config_file.chmod(0o600)

    info(f"writing {container_gitconfig} with {len(manifest_bottle.git)} insteadOf rule(s)")
    bottle.cp_in(str(config_file), container_gitconfig)
    bottle.exec(
        f"chown node:node {shlex.quote(container_gitconfig)} && "
        f"chmod 644 {shlex.quote(container_gitconfig)}",
        user="root",
    )


def _provision_git_user(plan: DockerBottlePlan, bottle: Bottle) -> None:
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
    manifest_bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    gu = manifest_bottle.git_user
    if gu.is_empty():
        return
    if gu.name:
        info(f"git config --global user.name = {gu.name!r}")
        bottle.exec(
            f"git config --global user.name {shlex.quote(gu.name)}",
            user="node",
        )
    if gu.email:
        info(f"git config --global user.email = {gu.email!r}")
        bottle.exec(
            f"git config --global user.email {shlex.quote(gu.email)}",
            user="node",
        )
