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
from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


# In-guest path mirrors the docker backend's claude-skills
# convention (~/.claude/skills/<name>/) under the node user's
# home — same path as the real bot-bottle image's
# /home/node/.claude/skills (pre-created in the Dockerfile).
_DEFAULT_SKILLS_DIR = "/home/node/.claude/skills"


def provision_skills(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Copy each of the agent's named skills from the host's
    ~/.claude/skills/<name>/ into the guest's equivalent path.
    For each skill: `mkdir -p` the destination, cp_in the host
    source dir over, then chown the result to node:node so the
    agent can read it. No-op when the agent has no skills.

    cp_in on a directory copies recursively; unlike docker cp's
    trailing-slash convention, smolvm doesn't need the `/.` suffix
    dance.

    cp_in lands files as root inside the VM, so we chown each
    skill tree over to node:node after the copy — same pattern as
    the docker backend's provision_prompt."""
    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    if not agent.skills:
        return

    skills_dir = os.environ.get(
        "BOT_BOTTLE_GUEST_SKILLS_DIR", _DEFAULT_SKILLS_DIR,
    )

    bottle.exec(f"mkdir -p {skills_dir}", user="root")

    for name in agent.skills:
        src = host_skill_dir(name)
        if not os.path.isdir(src):
            die(
                f"skill {name!r} disappeared from host between "
                f"validation and copy at {src}."
            )
        dst = f"{skills_dir}/{name}"
        info(f"copying skill {name} into {bottle.name}:{dst}")
        # Wipe any prior copy so re-runs don't accumulate.
        bottle.exec(f"rm -rf {dst}", user="root")
        bottle.cp_in(src, dst)
        bottle.exec(f"chown -R node:node {dst}", user="root")
