"""Copy the agent prompt into a running smolmachines bottle.

The prompt file is always copied (so the in-guest path always
exists) but `--append-system-prompt-file` only fires when the
agent actually has a prompt — the return value signals which
case, mirroring the docker backend's contract.

`smolvm machine cp` lands files as root inside the VM; the claude
process runs as `node`, so we chown + chmod the prompt after the
copy. Same flow as the docker backend's provision_prompt."""

from __future__ import annotations

import os

from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


# `node` is the agent user from the repo Dockerfile.
# BOT_BOTTLE_GUEST_HOME mirrors the docker backend's
# BOT_BOTTLE_CONTAINER_HOME knob.
_DEFAULT_GUEST_HOME = "/home/node"


def provision_prompt(plan: SmolmachinesBottlePlan, target: str) -> str | None:
    """Copy the prompt file into the running smolvm guest, fix
    ownership/mode. Returns the in-guest path if the agent has a
    non-empty prompt (drives --append-system-prompt-file), else
    None. The file is copied either way so the path always
    exists — mirrors the docker backend's behavior."""
    guest_home = os.environ.get("BOT_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)
    in_guest_prompt_path = f"{guest_home}/.bot-bottle-prompt.txt"

    _smolvm.machine_cp(str(plan.prompt_file), f"{target}:{in_guest_prompt_path}")
    # machine cp lands as root, source's 0o600 mode is preserved —
    # node can't read its own prompt without these two.
    _smolvm.machine_exec(target, ["chown", "node:node", in_guest_prompt_path])
    _smolvm.machine_exec(target, ["chmod", "600", in_guest_prompt_path])

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return in_guest_prompt_path if agent.prompt else None
