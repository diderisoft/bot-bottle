"""Agent provider runtime mapping.

The manifest owns the user-facing AgentProvider shape. This module is
the launch-time table that turns a provider template into an executable
command, default image, and prompt/auth behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .codex_auth import write_codex_dummy_auth_file


PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_TEMPLATES = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX})
PromptMode = Literal["append_file", "read_prompt_file"]


@dataclass(frozen=True)
class AgentProviderRuntime:
    template: str
    command: str
    image: str
    dockerfile: str
    auth_role: str
    placeholder_env: str
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


_REPO_ROOT = Path(__file__).resolve().parent.parent


_RUNTIMES = {
    PROVIDER_CLAUDE: AgentProviderRuntime(
        template=PROVIDER_CLAUDE,
        command="claude",
        image="bot-bottle-claude:latest",
        dockerfile=str(_REPO_ROOT / "Dockerfile.claude"),
        auth_role="claude_code_oauth",
        placeholder_env="CLAUDE_CODE_OAUTH_TOKEN",
        prompt_mode="append_file",
        bypass_args=("--dangerously-skip-permissions",),
        resume_args=("--continue",),
        remote_control_args=("--remote-control",),
    ),
    PROVIDER_CODEX: AgentProviderRuntime(
        template=PROVIDER_CODEX,
        command="codex",
        image="bot-bottle-codex:latest",
        dockerfile=str(_REPO_ROOT / "Dockerfile.codex"),
        auth_role="",
        placeholder_env="",
        prompt_mode="read_prompt_file",
        bypass_args=("--dangerously-bypass-approvals-and-sandbox",),
        resume_args=("resume", "--last"),
        remote_control_args=(),
    ),
}


def runtime_for(template: str) -> AgentProviderRuntime:
    return _RUNTIMES[template]


def agent_provision_plan(
    *,
    template: str,
    dockerfile: str,
    state_dir: Path,
    guest_home: str = "/home/node",
    guest_env: dict[str, str] | None = None,
    forward_host_credentials: bool = False,
    has_provider_auth: bool = False,
    host_env: dict[str, str] | None = None,
) -> AgentProvisionPlan:
    runtime = runtime_for(template)
    resolved_guest_env = dict(guest_env or {})
    env_vars: dict[str, str] = {}
    dirs: list[AgentProvisionDir] = []
    files: list[AgentProvisionFile] = []
    pre_copy: list[AgentProvisionCommand] = []
    verify: list[AgentProvisionCommand] = []

    if template == PROVIDER_CODEX:
        env_vars["CODEX_CA_CERTIFICATE"] = "/etc/ssl/certs/ca-certificates.crt"
        auth_dir = resolved_guest_env.get("CODEX_HOME", f"{guest_home}/.codex")
        if forward_host_credentials:
            env_vars["CODEX_HOME"] = auth_dir
        dirs.append(AgentProvisionDir(auth_dir))
        config_path = f"{auth_dir}/config.toml"
        config_file = state_dir / "codex-config.toml"
        config_file.write_text(
            f'[projects."{guest_home}"]\n'
            'trust_level = "trusted"\n'
        )
        config_file.chmod(0o600)
        files.append(AgentProvisionFile(config_file, config_path))

        if forward_host_credentials:
            auth_file = state_dir / "codex-auth.json"
            write_codex_dummy_auth_file(auth_file, host_env or dict(os.environ))
            files.append(AgentProvisionFile(auth_file, f"{auth_dir}/auth.json"))
            pre_copy.append(AgentProvisionCommand((
                "find", auth_dir,
                "-maxdepth", "1",
                "-type", "f",
                "(",
                "-name", "*.sqlite",
                "-o", "-name", "*.sqlite-*",
                "-o", "-name", "*.codex-repair-*.bak",
                ")",
                "-delete",
            ), "codex host credentials: could not reset runtime db files"))
            verify.append(AgentProvisionCommand((
                "runuser", "-u", "node", "--",
                "env",
                f"HOME={guest_home}",
                f"CODEX_HOME={auth_dir}",
                "codex", "login", "status",
            ), (
                "codex host credentials: dummy auth was copied into the "
                "guest, but Codex did not accept it"
            )))
    if template == PROVIDER_CLAUDE and has_provider_auth:
        env_vars["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env_vars["DISABLE_ERROR_REPORTING"] = "1"

    return AgentProvisionPlan(
        template=template,
        command=runtime.command,
        prompt_mode=runtime.prompt_mode,
        image=runtime.image,
        dockerfile=dockerfile,
        env_vars=env_vars,
        guest_env=resolved_guest_env,
        dirs=tuple(dirs),
        files=tuple(files),
        pre_copy=tuple(pre_copy),
        verify=tuple(verify),
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
