"""Plan type for the Firecracker backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...agent_provider import PromptMode
from ...manifest import ManifestImageOverlay
from .. import BottlePlan


@dataclass(frozen=True)
class FirecrackerBottlePlan(BottlePlan):
    slug: str
    forwarded_env: dict[str, str] = field(repr=False)
    # Stamped by launch once the gateway is up and its ports are
    # published on the host-side TAP IP (empty at prepare time).
    agent_proxy_url: str = ""
    agent_git_gate_url: str = ""
    agent_supervise_url: str = ""
    # Per-bottle identity token the agent presents on every attributed request
    # (egress proxy credentials, git-gate/supervise headers); set by launch
    # from the orchestrator registration. Empty pre-registration.
    identity_token: str = ""
    # Applied to every agent SSH exec and mirrored into /run inside the VM so
    # the host can recover it after the infra VM restarts.
    env_var_secret: str = ""

    @property
    def container_name(self) -> str:
        """Instance name, reused for the gateway container + VM run dir.
        Matches the `bot-bottle-<slug>` convention the other backends
        use so cleanup/enumerate discovery-by-prefix keeps working."""
        return self.agent_provision.instance_name

    @property
    def image(self) -> str:
        return self.agent_provision.image

    @property
    def dockerfile_path(self) -> str:
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

    @property
    def image_overlay(self) -> ManifestImageOverlay:
        return self.manifest.bottle.agent_provider.overlay

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
