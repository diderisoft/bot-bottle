"""Shared apply primitives used by every `AgentProvider` (PRD 0050).

The skills/prompt/provision loops here are backend-agnostic — they
only touch the `Bottle.exec`/`Bottle.cp_in` primitives that every
backend implements. Each provider's `provision_skills` /
`provision_prompt` / `provision` defaults delegate here so the
business logic lives in exactly one place."""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

from .agent_provider import GUEST_HOME
from .log import die, info


if TYPE_CHECKING:
    from .backend import Bottle, BottlePlan


# In-guest paths agents look at. These are baked into the bot-bottle
# Dockerfile and the smolmachines guest image; no env knob exposed,
# per the PRD 0050 issue feedback.
SKILLS_DIR = f"{GUEST_HOME}/.claude/skills"
PROMPT_PATH = f"{GUEST_HOME}/.bot-bottle-prompt.txt"


def apply_skills(plan: "BottlePlan", bottle: "Bottle") -> None:
    """Copy each named skill tree from the host into the guest.
    No-op when the agent has no skills.

    `cp_in` lands files as root in every backend; chown back to
    node:node so the agent can read its own skills."""
    from .backend.util import host_skill_dir

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    if not agent.skills:
        return

    bottle.exec(f"mkdir -p {SKILLS_DIR}", user="root")

    for name in agent.skills:
        src = host_skill_dir(name)
        if not os.path.isdir(src):
            die(
                f"skill {name!r} disappeared from host between "
                f"validation and copy at {src}."
            )
        dst = f"{SKILLS_DIR}/{name}"
        info(f"copying skill {name} into {bottle.name}:{dst}")
        bottle.exec(f"rm -rf {dst} && mkdir -p {dst}", user="root")
        bottle.cp_in(f"{src}/.", f"{dst}/")
        bottle.exec(f"chown -R node:node {dst}", user="root")


def apply_prompt(plan: "BottlePlan", bottle: "Bottle") -> str | None:
    """Copy the prompt file into the guest, fix ownership/mode.
    Returns the in-guest path iff the agent has a non-empty prompt
    (drives `--append-system-prompt-file`); the file is copied either
    way so the path always exists."""
    bottle.cp_in(str(plan.prompt_file), PROMPT_PATH)
    bottle.exec(
        f"chown node:node {PROMPT_PATH} && chmod 600 {PROMPT_PATH}",
        user="root",
    )
    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return PROMPT_PATH if agent.prompt else None


def apply_provision(plan: "BottlePlan", bottle: "Bottle") -> None:
    """Apply the declarative `dirs`/`pre_copy`/`files`/`verify` steps
    from the provider's `AgentProvisionPlan`. This is the loop that
    used to live in
    `backend/<name>/provision/provider_auth.py:provision_provider_auth`
    pre-PRD-0050."""
    provision = plan.agent_provision
    for d in provision.dirs:
        path = shlex.quote(d.guest_path)
        _exec(bottle, f"mkdir -p {path}", f"could not create {d.guest_path}")
        _exec(
            bottle,
            f"chown {shlex.quote(d.owner)} {path}",
            f"could not chown {d.guest_path}",
        )
        _exec(
            bottle,
            f"chmod {shlex.quote(d.mode)} {path}",
            f"could not chmod {d.guest_path}",
        )
    for command in provision.pre_copy:
        _exec(bottle, shlex.join(command.argv), command.error)
    for f in provision.files:
        bottle.cp_in(str(f.host_path), f.guest_path)
        path = shlex.quote(f.guest_path)
        _exec(
            bottle,
            f"chown {shlex.quote(f.owner)} {path}",
            f"could not chown {f.guest_path}",
        )
        _exec(
            bottle,
            f"chmod {shlex.quote(f.mode)} {path}",
            f"could not chmod {f.guest_path}",
        )
    for command in provision.verify:
        _exec(bottle, shlex.join(command.argv), command.error)


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
