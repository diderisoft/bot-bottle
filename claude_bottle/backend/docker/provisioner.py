"""DockerBottleProvisioner — copies prompt, skills, SSH keys, and
.git into a running Docker container.

Called by DockerBottleBackend.launch after the agent container is up
but before the DockerBottle handle is yielded. The returned in-
container prompt path tells the handle whether to add
--append-system-prompt-file to claude's argv.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ... import pipelock
from ... import skills as skills_mod
from ... import ssh as ssh_mod
from ...log import info
from .. import BottlePlan, BottleProvisioner
from .bottle_plan import DockerBottlePlan


class DockerBottleProvisioner(BottleProvisioner):
    """Docker implementation of BottleProvisioner."""

    def provision(self, plan: BottlePlan, target: str) -> str | None:
        assert isinstance(plan, DockerBottlePlan), (
            f"DockerBottleProvisioner.provision expects DockerBottlePlan, "
            f"got {type(plan).__name__}"
        )
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
        if agent.skills:
            skills_mod.skills_copy_into(container, list(agent.skills))

        bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
        if bottle.ssh:
            proxy_host_port = pipelock.pipelock_proxy_host_port(plan.slug)
            ssh_mod.ssh_setup(container, plan.stage_dir, proxy_host_port, bottle.ssh)

        if plan.spec.copy_cwd and Path(plan.spec.user_cwd, ".git").is_dir():
            info(f"copying {plan.spec.user_cwd}/.git -> {container}:/home/node/workspace/.git")
            subprocess.run(
                ["docker", "cp", f"{plan.spec.user_cwd}/.git", f"{container}:/home/node/workspace/.git"],
                stdout=subprocess.DEVNULL,
                check=True,
            )
            subprocess.run(
                [
                    "docker", "exec", "-u", "0", container,
                    "chown", "-R", "node:node", "/home/node/workspace/.git",
                ],
                stdout=subprocess.DEVNULL,
                check=True,
            )

        return in_container_prompt_path if agent.prompt else None
