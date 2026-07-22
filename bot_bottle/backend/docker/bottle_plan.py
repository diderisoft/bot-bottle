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
    # name -> value for vars forwarded into the docker-run child process
    # via subprocess env (so values never land on argv or in a file).
    # repr=False keeps secret/interpolated/OAuth values out of any
    # accidental log of the plan dataclass.
    forwarded_env: dict[str, str] = field(repr=False)
    use_runsc: bool
    # Consolidated mode (PRD 0070): the agent reaches git-gate over HTTP at the
    # shared gateway's address (`http://<gateway_ip>:9420`), set at launch.
    # Empty → single-tenant defaults (the `git-gate` alias over git://).
    agent_git_gate_url: str = ""
    # Likewise the supervise MCP endpoint at the gateway (`http://<gw>:9100/`);
    # empty → the single-tenant `supervise` alias.
    agent_supervise_url: str = ""
    # Per-bottle identity token the agent presents on every attributed request
    # (egress proxy credentials, git-gate/supervise headers); set by launch
    # from the orchestrator registration. Empty pre-registration.
    identity_token: str = ""
    # Encryption key for the agent's stored egress secrets; injected into the
    # agent container as ENV_VAR_SECRET via the compose subprocess env (bare
    # name — value never written to the compose file). Empty pre-registration.
    env_var_secret: str = ""

    @property
    def container_name(self) -> str:
        return self.agent_provision.instance_name

    @property
    def git_gate_insteadof_host(self) -> str:
        if self.agent_git_gate_url.startswith("http://"):
            return self.agent_git_gate_url.removeprefix("http://").rstrip("/")
        return super().git_gate_insteadof_host

    @property
    def git_gate_insteadof_scheme(self) -> str:
        if self.agent_git_gate_url.startswith("http://"):
            return "http"
        return super().git_gate_insteadof_scheme

    @property
    def image(self) -> str:
        return self.agent_provision.image

    @property
    def dockerfile_path(self) -> str:
        """Absolute path to the Dockerfile that builds `image`. Sourced
        from the agent provision plan — the manifest may override per
        bottle; otherwise the provider plugin's bundled Dockerfile."""
        return self.agent_provision.dockerfile

    @property
    def prompt_file(self) -> Path:
        return self.agent_provision.prompt_file

    @property
    def agent_command(self) -> str:
        return self.agent_provision.command

    @property
    def agent_prompt_mode(self) -> PromptMode:
        return self.agent_provision.prompt_mode

    @property
    def agent_provider_template(self) -> str:
        return self.agent_provision.template
