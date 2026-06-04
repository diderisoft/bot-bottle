"""Agent provider runtime mapping.

The manifest owns the user-facing AgentProvider shape. This module is
the launch-time table that turns a provider template into an executable
command, default image, and prompt/auth behavior.

Per PRD 0050 the per-provider implementations live under
`bot_bottle/contrib/<template>/agent_provider.py`. This module exposes:

  - `AgentProvider` (ABC) — the contract each plugin implements.
  - `get_provider(template)` — lazy-imported registry; the analogue
    of `bot_bottle/deploy_key_provisioner.get_provisioner`.
  - `AgentProvisionPlan` (+ helper dataclasses) — declarative shape
    each provider produces and the backends consume unchanged.
  - `agent_provision_plan` / `runtime_for` — thin wrappers around the
    registry kept so existing callers keep working without per-call
    edits.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .egress import EgressRoute


if TYPE_CHECKING:
    from .backend import Bottle, BottlePlan


PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_TEMPLATES = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX})

# Hosts that egress injects the host ChatGPT bearer on when Codex
# forward_host_credentials is enabled. Pipelock must pass these through
# (no TLS MITM) or its header DLP blocks the injected JWT.
CODEX_HOST_CREDENTIAL_HOSTS = ("api.openai.com", "chatgpt.com")
PromptMode = Literal["append_file", "read_prompt_file"]

GUEST_HOME = "/home/node"


@dataclass(frozen=True)
class AgentProviderRuntime:
    template: str
    command: str
    image: str
    dockerfile: str
    prompt_mode: PromptMode
    bypass_args: tuple[str, ...]
    resume_args: tuple[str, ...]
    remote_control_args: tuple[str, ...]


@dataclass(frozen=True)
class AgentProvisionDir:
    guest_path: str
    mode: str = "700"
    owner: str = "node:node"


@dataclass(frozen=True)
class AgentProvisionFile:
    host_path: Path
    guest_path: str
    mode: str = "600"
    owner: str = "node:node"


@dataclass(frozen=True)
class AgentProvisionCommand:
    argv: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class AgentProvisionPlan:
    """Provider-owned guest setup.

    Backends interpret this plan with their own copy/exec primitives.
    Provider-specific content stays here so future provider plugins can
    return the same shape without adding backend-plan fields.

    `egress_routes` are provider-declared EgressRoutes that backends
    pass to `Egress.prepare` and `PipelockProxy.prepare`. This keeps
    provider logic out of the egress and pipelock modules — they merge
    provider routes generically without knowing the provider type.

    `hidden_env_names` is the set of env var names the provider injected
    as non-secret placeholders. `print_util.visible_agent_env_names` uses
    this to suppress them from the preflight summary so operators don't
    mistake them for real credentials.
    """

    template: str
    command: str
    prompt_mode: PromptMode
    image: str
    dockerfile: str
    guest_env: dict[str, str]
    env_vars: dict[str, str] = field(default_factory=dict)
    dirs: tuple[AgentProvisionDir, ...] = ()
    files: tuple[AgentProvisionFile, ...] = ()
    pre_copy: tuple[AgentProvisionCommand, ...] = ()
    verify: tuple[AgentProvisionCommand, ...] = ()
    egress_routes: tuple[EgressRoute, ...] = ()
    hidden_env_names: frozenset[str] = field(default_factory=frozenset)
    provisioned_env: dict[str, str] = field(default_factory=dict)


class AgentProvider(ABC):
    """Per-template plugin: produces the provision plan and applies
    the provider-specific in-guest setup steps (skills, prompt, the
    declarative `dirs`/`files`/`pre_copy`/`verify` apply loop, and
    supervise MCP registration). Concrete subclasses live under
    `bot_bottle/contrib/<template>/agent_provider.py`."""

    @property
    @abstractmethod
    def runtime(self) -> AgentProviderRuntime:
        """The static command / image / prompt-mode table for this
        template."""

    @abstractmethod
    def provision_plan(
        self,
        *,
        dockerfile: str,
        state_dir: Path,
        guest_home: str = GUEST_HOME,
        guest_env: dict[str, str] | None = None,
        auth_token: str = "",
        forward_host_credentials: bool = False,
        host_env: dict[str, str] | None = None,
        trusted_project_path: str = "",
    ) -> AgentProvisionPlan:
        """Build the declarative AgentProvisionPlan for one launch.
        Backends call this during `prepare` and consume the result as
        before."""

    @abstractmethod
    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy each of the agent's named skills from the host into
        the guest. No-op when the agent has no skills. The in-guest
        layout is provider-specific (claude-code's
        `~/.claude/skills/` today; future providers may differ)."""

    @abstractmethod
    def provision_prompt(self, plan: "BottlePlan", bottle: "Bottle") -> str | None:
        """Copy the prompt file into the guest, fix ownership/mode,
        and return the in-guest path iff the agent has a non-empty
        prompt (drives the `--append-system-prompt-file` flag).

        The file is copied either way so the path always exists."""

    @abstractmethod
    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Apply the provider's declarative
        `dirs`/`pre_copy`/`files`/`verify` steps from
        `plan.agent_provision`. Was called `provision_provider_auth`
        on `BottleBackend` before PRD 0050."""

    @abstractmethod
    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        """Register the per-bottle supervise sidecar as an MCP server
        in the provider's in-guest config. Called by the backend after
        the supervise sidecar is reachable. No-op when
        `plan.supervise_plan is None`."""


def get_provider(template: str) -> AgentProvider:
    """Resolve a provider template name to its plugin instance.

    Lazy-imports the contrib module so importing this module doesn't
    pull provider-specific code paths in. Mirrors the contrib
    convention PRD 0048 established for deploy key provisioners."""
    if template == PROVIDER_CLAUDE:
        from .contrib.claude.agent_provider import ClaudeAgentProvider
        return ClaudeAgentProvider()
    if template == PROVIDER_CODEX:
        from .contrib.codex.agent_provider import CodexAgentProvider
        return CodexAgentProvider()
    raise ValueError(f"unknown agent provider template: {template!r}")


def runtime_for(template: str) -> AgentProviderRuntime:
    return get_provider(template).runtime


def agent_provision_plan(
    *,
    template: str,
    dockerfile: str,
    state_dir: Path,
    guest_home: str = GUEST_HOME,
    guest_env: dict[str, str] | None = None,
    auth_token: str = "",
    forward_host_credentials: bool = False,
    host_env: dict[str, str] | None = None,
    trusted_project_path: str = "",
) -> AgentProvisionPlan:
    """Back-compat shim — `prepare` callers stay the same; the work
    now lives on the provider plugin."""
    return get_provider(template).provision_plan(
        dockerfile=dockerfile,
        state_dir=state_dir,
        guest_home=guest_home,
        guest_env=guest_env,
        auth_token=auth_token,
        forward_host_credentials=forward_host_credentials,
        host_env=host_env,
        trusted_project_path=trusted_project_path,
    )


def prompt_args(
    prompt_mode: PromptMode,
    prompt_path: str | None,
    *,
    argv: list[str] | None = None,
) -> list[str]:
    if not prompt_path:
        return []
    if prompt_mode == "append_file":
        return ["--append-system-prompt-file", prompt_path]
    if prompt_mode == "read_prompt_file":
        if argv and "resume" in argv:
            return []
        return [f"Read and follow the instructions in {prompt_path}."]
    raise ValueError(f"unknown provider prompt mode: {prompt_mode}")
