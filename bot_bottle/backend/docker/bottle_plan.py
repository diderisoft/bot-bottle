"""DockerBottlePlan — concrete subclass of BottlePlan.

Carries the Docker-specific resolved fields produced by
DockerBottleBackend.prepare. The launch step consumes it without
further resolution; preflight rendering is inherited from BottlePlan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...agent_provider import PromptMode
from .. import BottlePlan


@dataclass(frozen=True)
class DockerBottlePlan(BottlePlan):
    """Docker-specific resolved fields produced by
    DockerBottleBackend.prepare. Inherits `spec`, `stage_dir`,
    `git_gate_plan`, `egress_plan`, `supervise_plan`, and
    `agent_provision` from BottlePlan."""

    slug: str
    container_name: str
    container_name_pinned: bool
    image: str
    derived_image: str           # "" -> no derived image
    runtime_image: str           # image actually launched (derived or base)
    # Absolute path to the Dockerfile that builds `image`. Empty means
    # use the repo's default Dockerfile. Populated to a per-bottle
    # state file (~/.bot-bottle/state/<slug>/Dockerfile) after a
    # capability-block remediation (PRD 0016).
    dockerfile_path: str
    env_file: Path               # docker --env-file: NAME=VALUE literals
    # name -> value for vars forwarded into the docker-run child process
    # via subprocess env (so values never land on argv or in a file).
    # repr=False keeps secret/interpolated/OAuth values out of any
    # accidental log of the plan dataclass.
    forwarded_env: dict[str, str] = field(repr=False)
    prompt_file: Path
    use_runsc: bool

    @property
    def agent_command(self) -> str:
        return self.agent_provision.command

    @property
    def agent_prompt_mode(self) -> PromptMode:
        return self.agent_provision.prompt_mode

    @property
    def agent_provider_template(self) -> str:
        return self.agent_provision.template
