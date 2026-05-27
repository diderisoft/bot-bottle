"""Copy the agent prompt into a running smolmachines bottle.

The prompt file is always copied (so the in-guest path always
exists) but `--append-system-prompt-file` only fires when the
agent actually has a prompt — the return value signals which
case, mirroring the docker backend's contract."""

from __future__ import annotations

from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


# In-guest path for the prompt. Smolvm's default agent image
# (alpine for now; the real claude-bottle image later) runs as
# root with $HOME=/root. The path is also surfaced as the return
# value so the caller can pass it via --append-system-prompt-file.
_IN_GUEST_PROMPT_PATH = "/root/.claude-bottle-prompt.txt"


def provision_prompt(plan: SmolmachinesBottlePlan, target: str) -> str | None:
    """Copy the prompt file into the running smolvm guest. Returns
    the in-guest path if the agent has a non-empty prompt (drives
    --append-system-prompt-file), else None. The file is copied
    either way so the path always exists — mirrors the docker
    backend's behavior."""
    _smolvm.machine_cp(
        str(plan.prompt_file),
        f"{target}:{_IN_GUEST_PROMPT_PATH}",
    )
    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    return _IN_GUEST_PROMPT_PATH if agent.prompt else None
