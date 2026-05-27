"""Copy host-side skill directories into a running smolmachines
bottle.

Skills are validated on the host before launch by
`BottleBackend._validate_skills`; this module assumes that
validation has already run. A skill that disappears between
validation and copy still dies loudly rather than silently
producing a partial guest."""

from __future__ import annotations

import os

from ....log import die, info
from ...util import host_skill_dir
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


# In-guest path mirrors the docker backend's claude-skills
# convention (~/.claude/skills/<name>/). For smolmachines the
# agent is root by default; chunk 5+ may swap to a node user
# in the real claude-bottle image, at which point this path
# follows /home/node/ — the env knob below provides the override.
_DEFAULT_SKILLS_DIR = "/root/.claude/skills"


def provision_skills(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Copy each of the agent's named skills from the host's
    ~/.claude/skills/<name>/ into the guest's equivalent path.
    For each skill: `mkdir -p` the destination, then
    `smolvm machine cp` the host source dir over. No-op when the
    agent has no skills.

    smolvm machine cp on a directory copies recursively (same
    semantics as `cp -r`); unlike docker cp's trailing-slash
    convention, smolvm doesn't need the `/.` suffix dance."""
    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    if not agent.skills:
        return

    skills_dir = os.environ.get(
        "CLAUDE_BOTTLE_GUEST_SKILLS_DIR", _DEFAULT_SKILLS_DIR,
    )

    _smolvm.machine_exec(target, ["mkdir", "-p", skills_dir])

    for name in agent.skills:
        src = host_skill_dir(name)
        if not os.path.isdir(src):
            die(
                f"skill {name!r} disappeared from host between "
                f"validation and copy at {src}."
            )
        dst = f"{skills_dir}/{name}"
        info(f"copying skill {name} into {target}:{dst}")
        # Wipe any prior copy so re-runs don't accumulate.
        _smolvm.machine_exec(target, ["rm", "-rf", dst])
        _smolvm.machine_cp(src, f"{target}:{dst}")
