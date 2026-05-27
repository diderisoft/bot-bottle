"""Git provisioning inside a running smolmachines bottle
(PRD 0023 chunk 4d).

Two concerns, both about git in the agent:

  1. If --cwd was passed AND the host cwd has a .git, copy that
     .git into /home/node/workspace/.git so the agent operates on
     the user's repo.
  2. If the bottle declares `git` entries (PRD 0008), write a
     ~/.gitconfig with insteadOf rules so every git operation
     against a declared upstream transparently hits the per-bottle
     git-gate. The gate mirrors the upstream in both directions,
     so URL rewriting is symmetric.

Differs from `backend.docker.provision.git` in one address detail:
the TSI-allowlisted guest can only reach the bundle's pinned IP
(no DNS resolver in the /32 allowlist), so the insteadOf URLs
are `git://<bundle_ip>:<port>/<name>.git` rather than the
docker backend's `git://git-gate/<name>.git`. The render itself
is the shared `git_gate_render_gitconfig` on the platform-neutral
git_gate module."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ....git_gate import git_gate_render_gitconfig
from ....log import info
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


# `node` is the agent user from the repo Dockerfile. Override via
# CLAUDE_BOTTLE_GUEST_HOME mirrors the docker backend's
# CLAUDE_BOTTLE_CONTAINER_HOME knob — same purpose, different
# transport.
_DEFAULT_GUEST_HOME = "/home/node"


def _guest_home() -> str:
    return os.environ.get("CLAUDE_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)


def provision_git(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Set up git inside the guest. Runs both subcases; each
    no-ops when its condition isn't met."""
    _provision_cwd_git(plan, target)
    _provision_git_gate_config(plan, target)


def _provision_cwd_git(plan: SmolmachinesBottlePlan, target: str) -> None:
    """If --cwd was set and the host cwd has a .git directory, copy
    it into <guest_home>/workspace/.git and fix ownership. No-op
    otherwise."""
    if not (plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir()):
        return
    guest_workspace_git = f"{_guest_home()}/workspace/.git"
    info(f"copying {plan.spec.user_cwd}/.git -> {target}:{guest_workspace_git}")
    # mkdir -p the workspace dir so `machine cp` lands the .git
    # directly there even on first-time bottles.
    _smolvm.machine_exec(target, ["mkdir", "-p", f"{_guest_home()}/workspace"])
    _smolvm.machine_cp(
        f"{plan.spec.user_cwd}/.git", f"{target}:{guest_workspace_git}",
    )
    # `machine cp` lands files as root; the agent runs as node so
    # the workspace tree must be chowned over.
    _smolvm.machine_exec(
        target, ["chown", "-R", "node:node", guest_workspace_git],
    )


def _provision_git_gate_config(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Write ~/.gitconfig in the guest with the git-gate insteadOf
    rules. No-op when the bottle has no `git` entries."""
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not bottle.git:
        return

    # `127.0.0.1:<host port>` form: the bundle's git-gate port
    # is published on host loopback at launch time so the
    # smolvm guest (which can only reach macOS networking via
    # TSI, not the docker bridge IP) can dial it. launch.py
    # populates `plan.agent_git_gate_host` after bundle bringup.
    content = git_gate_render_gitconfig(bottle.git, plan.agent_git_gate_host)

    guest_gitconfig = f"{_guest_home()}/.gitconfig"
    # Stage the file under the plan's stage_dir so `machine cp`
    # has a stable host path. The plan's stage_dir is cleaned up
    # by start.py's session-end teardown.
    with tempfile.NamedTemporaryFile(
        "w", dir=str(plan.stage_dir), prefix="gitconfig.",
        delete=False,
    ) as f:
        f.write(content)
        config_file = Path(f.name)
    os.chmod(config_file, 0o600)

    info(f"writing {guest_gitconfig} with {len(bottle.git)} insteadOf rule(s)")
    _smolvm.machine_cp(str(config_file), f"{target}:{guest_gitconfig}")
    _smolvm.machine_exec(target, ["chown", "node:node", guest_gitconfig])
    _smolvm.machine_exec(target, ["chmod", "644", guest_gitconfig])
