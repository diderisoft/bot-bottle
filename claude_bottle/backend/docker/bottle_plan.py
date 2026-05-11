"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; show_plan-style rendering is the `print` method.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ...log import info
from .. import BottlePlan


@dataclass(frozen=True)
class DockerBottlePlan(BottlePlan):
    """Docker-specific resolved fields produced by
    DockerBottleBackend.prepare. Inherits `spec` and `stage_dir` from
    BottlePlan."""

    slug: str
    container_name: str
    container_name_pinned: bool
    image: str
    derived_image: str           # "" -> no derived image
    runtime_image: str           # image actually launched (derived or base)
    env_file: Path
    args_file: Path
    prompt_file: Path
    pipelock_yaml_path: Path
    pipelock_yaml_filename: str
    allowlist_summary: str
    use_runsc: bool

    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr. Pure presentation."""
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)

        env_names = list(bottle.env.keys())
        if spec.forward_oauth_token:
            env_names.append("CLAUDE_CODE_OAUTH_TOKEN")

        ssh_hosts = [e.Host for e in bottle.ssh]
        prompt_first_line = agent.prompt.splitlines()[0] if agent.prompt else ""
        runtime_label = "runsc (gVisor)" if self.use_runsc else "runc (default)"

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        info(f"image           : {self.image}")
        if self.derived_image:
            info(
                f"cwd             : {spec.user_cwd} -> /home/node/workspace "
                f"(derived: {self.derived_image})"
            )
        info(f"container       : {self.container_name}")
        info(f"stage dir       : {self.stage_dir}")
        info("env (names only): " + (", ".join(env_names) if env_names else "(none)"))
        info("skills          : " + (" ".join(agent.skills) if agent.skills else "(none)"))
        info(f"docker runtime  : {runtime_label}")
        info(f"bottle             : {agent.bottle}")
        if ssh_hosts:
            info(f"  ssh hosts     : {', '.join(ssh_hosts)}")
        else:
            info("  ssh hosts     : (none)")
        info(f"  egress        : {self.allowlist_summary}")
        info(
            f"prompt          : {len(agent.prompt)} chars; "
            f"first line: {prompt_first_line or '(empty)'}"
        )
        info("remote-control  : " + ("enabled" if remote_control else "disabled"))
        print(file=sys.stderr)
