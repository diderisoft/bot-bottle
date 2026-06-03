"""Copy the agent prompt into a running Docker bottle.

The prompt file is always copied (so the in-container path always
exists) but `--append-system-prompt-file` only fires when the agent
actually has a prompt — the return value signals which case."""

from __future__ import annotations

import os

from ... import Bottle
from ..bottle_plan import DockerBottlePlan


def provision_prompt(plan: DockerBottlePlan, bottle: Bottle) -> str | None:
    """Copy the prompt file into the container, fix ownership/mode.
    Returns the in-container path if the agent has a non-empty
    prompt (drives --append-system-prompt-file), else None. The
    file is copied either way so the path always exists."""
    container_home = os.environ.get("BOT_BOTTLE_CONTAINER_HOME", "/home/node")
    in_container_prompt_path = f"{container_home}/.bot-bottle-prompt.txt"

    bottle.cp_in(str(plan.prompt_file), in_container_prompt_path)
    # `docker cp` preserves host UID; re-own/mode as root so node
    # can read its own mode-600 prompt regardless of host UID.
    bottle.exec(
        f"chown node:node {in_container_prompt_path} && "
        f"chmod 600 {in_container_prompt_path}",
        user="root",
    )

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return in_container_prompt_path if agent.prompt else None
