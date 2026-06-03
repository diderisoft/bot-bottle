"""Copy the agent prompt into a running smolmachines bottle.

The prompt file is always copied (so the in-guest path always
exists) but `--append-system-prompt-file` only fires when the
agent actually has a prompt — the return value signals which
case, mirroring the docker backend's contract.

cp_in lands files as root inside the VM; the claude
process runs as `node`, so we chown + chmod the prompt after the
copy. Same flow as the docker backend's provision_prompt."""

from __future__ import annotations

import os

from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


# `node` is the agent user from the repo Dockerfile.
# BOT_BOTTLE_GUEST_HOME mirrors the docker backend's
# BOT_BOTTLE_CONTAINER_HOME knob.
_DEFAULT_GUEST_HOME = "/home/node"


def provision_prompt(plan: SmolmachinesBottlePlan, bottle: Bottle) -> str | None:
    """Copy the prompt file into the running smolvm guest, fix
    ownership/mode. Returns the in-guest path if the agent has a
    non-empty prompt (drives --append-system-prompt-file), else
    None. The file is copied either way so the path always
    exists — mirrors the docker backend's behavior."""
    guest_home = os.environ.get("BOT_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)
    in_guest_prompt_path = f"{guest_home}/.bot-bottle-prompt.txt"

    bottle.cp_in(str(plan.prompt_file), in_guest_prompt_path)
    # cp_in lands as root, source's 0o600 mode is preserved —
    # node can't read its own prompt without these two.
    bottle.exec(
        f"chown node:node {in_guest_prompt_path} && chmod 600 {in_guest_prompt_path}",
        user="root",
    )

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return in_guest_prompt_path if agent.prompt else None
