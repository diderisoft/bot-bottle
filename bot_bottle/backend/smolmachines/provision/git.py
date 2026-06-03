"""Git provisioning inside a running smolmachines bottle
(PRD 0023 chunk 4d).

Three concerns, all about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that
     .git into the planned guest workspace so the agent operates on
     the user's repo.
  2. If the bottle declares `git` entries (PRD 0008), write a
     ~/.gitconfig with insteadOf rules so every git operation
     against a declared upstream transparently hits the per-bottle
     git-gate. The gate mirrors the upstream in both directions,
     so URL rewriting is symmetric.
  3. If the bottle declares `git.user` (issue #86), set
     `git config --global user.{name,email}` inside the guest so
     the agent's commits are attributed to that identity.

Differs from `backend.docker.provision.git` in one address detail:
the TSI-allowlisted guest can only reach the bundle's pinned IP
(no DNS resolver in the /32 allowlist), so the insteadOf URLs
are `http://<bundle_ip>:<port>/<name>.git` rather than the
docker backend's `git://git-gate/<name>.git`. The render itself
is the shared `git_gate_render_gitconfig` on the platform-neutral
git_gate module."""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from ....git_gate import git_gate_render_gitconfig
from ....log import info
from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


# `node` is the agent user from the repo Dockerfile. Override via
# BOT_BOTTLE_GUEST_HOME mirrors the docker backend's
# BOT_BOTTLE_CONTAINER_HOME knob — same purpose, different
# transport.
_DEFAULT_GUEST_HOME = "/home/node"


def _guest_home() -> str:
    return os.environ.get("BOT_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)


def provision_git(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Set up git inside the guest. Runs all three subcases; each
    no-ops when its condition isn't met."""
    _provision_cwd_git(plan, bottle)
    _provision_git_gate_config(plan, bottle)
    _provision_git_user(plan, bottle)


def _provision_cwd_git(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """If --cwd was set and the host cwd has a .git directory, copy
    it into <guest_home>/workspace/.git and fix ownership. No-op
    otherwise."""
    workspace = plan.workspace_plan
    if not (workspace.enabled and workspace.copy_git and workspace.has_host_git_dir):
        return
    guest_workspace_git = f"{workspace.guest_path}/.git"
    host_git = str(workspace.host_path / ".git")
    info(f"copying {host_git} -> {bottle.name}:{guest_workspace_git}")
    # mkdir -p the workspace dir so cp_in lands the .git
    # directly there even on first-time bottles.
    bottle.exec(f"mkdir -p {shlex.quote(workspace.guest_path)}", user="root")
    bottle.cp_in(host_git, guest_workspace_git)
    # cp_in lands files as root; the agent runs as node so
    # the workspace tree must be chowned over.
    bottle.exec(
        f"chown -R {shlex.quote(workspace.owner)} {shlex.quote(guest_workspace_git)}",
        user="root",
    )


def _provision_git_gate_config(
    plan: SmolmachinesBottlePlan, bottle: Bottle
) -> None:
    """Write ~/.gitconfig in the guest with the git-gate insteadOf
    rules. No-op when the bottle has no `git` entries."""
    manifest_bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not manifest_bottle.git:
        return

    # `<loopback alias>:<host port>` form: the bundle's git-gate
    # HTTP port is published on host loopback at launch time so
    # the smolvm guest (which can only reach macOS networking via
    # TSI, not the docker bridge IP) can dial it. launch.py
    # populates `plan.agent_git_gate_host` after bundle bringup.
    content = git_gate_render_gitconfig(
        manifest_bottle.git, plan.agent_git_gate_host, scheme="http",
    )

    guest_gitconfig = f"{_guest_home()}/.gitconfig"
    # Stage the file under the plan's stage_dir so cp_in
    # has a stable host path. The plan's stage_dir is cleaned up
    # by start.py's session-end teardown.
    with tempfile.NamedTemporaryFile(
        "w", dir=str(plan.stage_dir), prefix="gitconfig.",
        delete=False,
    ) as f:
        f.write(content)
        config_file = Path(f.name)
    os.chmod(config_file, 0o600)

    info(f"writing {guest_gitconfig} with {len(manifest_bottle.git)} insteadOf rule(s)")
    bottle.cp_in(str(config_file), guest_gitconfig)
    bottle.exec(
        f"chown node:node {shlex.quote(guest_gitconfig)} && "
        f"chmod 644 {shlex.quote(guest_gitconfig)}",
        user="root",
    )


def _provision_git_user(
    plan: SmolmachinesBottlePlan, bottle: Bottle,
) -> None:
    """Apply `git config --global user.{name,email}` inside the
    guest as the node user so --global lands in the same
    `/home/node/.gitconfig` that `_provision_git_gate_config`
    writes to. No-op when the bottle didn't declare `git.user`.

    SmolmachinesBottle.exec(user="node") automatically sets
    HOME=/home/node so --global writes to /home/node/.gitconfig."""
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
