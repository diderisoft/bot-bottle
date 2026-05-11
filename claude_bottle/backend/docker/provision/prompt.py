"""Copy the agent prompt into a running Docker bottle.

The prompt file is always copied (so the in-container path always
exists) but `--append-system-prompt-file` only fires when the agent
actually has a prompt — the return value signals which case."""

from __future__ import annotations

import os
import subprocess

from ..bottle_plan import DockerBottlePlan


def provision_prompt(plan: DockerBottlePlan, target: str) -> str | None:
    """Copy the prompt file into the container, fix ownership/mode.
    Returns the in-container path if the agent has a non-empty
    prompt (drives --append-system-prompt-file), else None. The
    file is copied either way so the path always exists."""
    container = target
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    in_container_prompt_path = f"{container_home}/.claude-bottle-prompt.txt"

    subprocess.run(
        ["docker", "cp", str(plan.prompt_file), f"{container}:{in_container_prompt_path}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    # `docker cp` preserves host UID; re-own/mode as root so node
    # can read its own mode-600 prompt regardless of host UID.
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chown", "node:node", in_container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "chmod", "600", in_container_prompt_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return in_container_prompt_path if agent.prompt else None
