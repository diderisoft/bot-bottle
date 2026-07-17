"""Plan type for the macOS Apple Container backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ...agent_provider import PromptMode
from .. import BottlePlan


@dataclass(frozen=True)
class MacosContainerBottlePlan(BottlePlan):
    slug: str
    forwarded_env: dict[str, str] = field(repr=False)
    agent_git_gate_url: str = ""
    agent_supervise_url: str = ""
    # Read by provision-time consumers (git extraHeader, supervise MCP header)
    # via getattr(plan, "identity_token", ""); stamped in launch after the
    # bottle is registered. See launch.py's stamp for why it lives here and not
    # only in the exec-time proxy env.
    identity_token: str = ""

    @property
    def container_name(self) -> str:
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
    def git_gate_insteadof_host(self) -> str:
        if self.agent_git_gate_url.startswith("http://"):
            return self.agent_git_gate_url.removeprefix("http://").rstrip("/")
        return super().git_gate_insteadof_host

    @property
    def git_gate_insteadof_scheme(self) -> str:
        if self.agent_git_gate_url.startswith("http://"):
            return "http"
        return super().git_gate_insteadof_scheme
