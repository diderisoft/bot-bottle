"""Claude agent provider plugin (PRD 0050, contrib).

The Claude-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (claude.json trust marker,
api.anthropic.com egress route, OAuth-token placeholder), plus
the `claude mcp add` invocation that registers the supervise
sidecar in claude-code's user config (PRD 0013)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    GUEST_HOME,
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from ...egress import EgressRoute
from ...log import info, warn


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_REPO_ROOT = Path(__file__).resolve().parents[3]

_SUPERVISE_MCP_NAME = "supervise"

_RUNTIME = AgentProviderRuntime(
    template="claude",
    command="claude",
    image="bot-bottle-claude:latest",
    dockerfile=str(_REPO_ROOT / "Dockerfile.claude"),
    prompt_mode="append_file",
    bypass_args=("--dangerously-skip-permissions",),
    resume_args=("--continue",),
    remote_control_args=("--remote-control",),
)


class ClaudeAgentProvider(AgentProvider):
    @property
    def runtime(self) -> AgentProviderRuntime:
        return _RUNTIME

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
        del forward_host_credentials, host_env  # Codex-only knobs
        resolved_guest_env = dict(guest_env or {})
        trusted_path = trusted_project_path or guest_home

        env_vars: dict[str, str] = {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        claude_config = state_dir / "claude.json"
        claude_projects = {guest_home: {"hasTrustDialogAccepted": True}}
        claude_projects[trusted_path] = {"hasTrustDialogAccepted": True}
        claude_config.write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "bypassPermissionsModeAccepted": True,
            "projects": claude_projects,
        }, indent=2) + "\n")
        claude_config.chmod(0o600)
        files = (
            AgentProvisionFile(claude_config, f"{guest_home}/.claude.json"),
        )
        egress_routes = (EgressRoute(
            host="api.anthropic.com",
            auth_scheme="Bearer" if auth_token else "",
            token_ref=auth_token,
            tls_passthrough=True,
        ),)
        hidden_env_names: frozenset[str] = frozenset()
        if auth_token:
            env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = "egress-placeholder"
            hidden_env_names = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

        return AgentProvisionPlan(
            template=_RUNTIME.template,
            command=_RUNTIME.command,
            prompt_mode=_RUNTIME.prompt_mode,
            image=_RUNTIME.image,
            dockerfile=dockerfile,
            env_vars=env_vars,
            guest_env=resolved_guest_env,
            files=files,
            egress_routes=egress_routes,
            hidden_env_names=hidden_env_names,
        )

    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        """Run `claude mcp add` inside the agent guest to register the
        supervise sidecar in claude-code's user config (~/.claude.json).

        Failure is logged but not fatal — the bottle still works without
        the entry; the operator can register it manually."""
        if plan.supervise_plan is None:
            return
        info(f"registering supervise MCP server in agent claude config → {supervise_url}")
        r = bottle.exec(
            f"claude mcp add --scope user --transport http "
            f"{_SUPERVISE_MCP_NAME} {supervise_url}",
            user="node",
        )
        if r.returncode != 0:
            warn(
                f"`claude mcp add supervise` failed (exit {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
                f"register manually with: "
                f"claude mcp add --scope user --transport http supervise {supervise_url}"
            )
