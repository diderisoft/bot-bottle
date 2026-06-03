"""Copy host-side skill directories into a running Docker bottle.

Skills are validated on the host before launch by the base class's
`BottleBackend._validate_skills` (called from `prepare`); this module
assumes that validation has already run. A skill disappearing between
validation and copy still dies loudly rather than silently producing
a partial container."""

from __future__ import annotations

import os

from ....log import die, info
from ...util import host_skill_dir
from ... import Bottle
from ..bottle_plan import DockerBottlePlan


def provision_skills(plan: DockerBottlePlan, bottle: Bottle) -> None:
    """Copy each of the agent's named skills from the host's
    ~/.claude/skills/<name>/ into the container's equivalent path.
    For each skill: ensure parent dir, wipe any prior copy, then
    `cp_in <host>/. <container>:<dst>/` so the contents are
    copied into a freshly-created destination dir. No-op when the
    agent has no skills."""
    agent = plan.spec.manifest.agents[plan.spec.agent_name]
    if not agent.skills:
        return

    container_home = os.environ.get("BOT_BOTTLE_CONTAINER_HOME", "/home/node")
    skills_dir = os.environ.get(
        "BOT_BOTTLE_CONTAINER_SKILLS_DIR", f"{container_home}/.claude/skills"
    )

    bottle.exec(f"mkdir -p {skills_dir}", user="node")

    for n in agent.skills:
        src = host_skill_dir(n)
        if not os.path.isdir(src):
            die(f"skill '{n}' disappeared from host between validation and copy at {src}.")
        dst = f"{skills_dir}/{n}"
        info(f"copying skill {n} into {bottle.name}:{dst}")
        bottle.exec(f"rm -rf {dst} && mkdir -p {dst}", user="node")
        bottle.cp_in(f"{src}/.", f"{dst}/")
