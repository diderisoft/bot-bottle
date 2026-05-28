"""Agent provider runtime mapping.

The manifest owns the user-facing AgentProvider shape. This module is
the launch-time table that turns a provider template into an executable
command, default image, and prompt/auth behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


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
        auth_role="codex_auth",
        placeholder_env="OPENAI_API_KEY",
        prompt_mode="read_prompt_file",
        bypass_args=("--dangerously-bypass-approvals-and-sandbox",),
        resume_args=("resume", "--last"),
        remote_control_args=(),
    ),
}


def runtime_for(template: str) -> AgentProviderRuntime:
    return _RUNTIMES[template]


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
