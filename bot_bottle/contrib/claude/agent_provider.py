"""Claude agent provider plugin (PRD 0050, contrib).

The Claude-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (claude.json trust marker,
api.anthropic.com egress route, OAuth-token placeholder), plus
the `claude mcp add` invocation that registers the supervise
sidecar in claude-code's user config (PRD 0013)."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from ...egress import EgressRoute
from ...log import die, info, warn


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_SUPERVISE_MCP_NAME = "supervise"


def _skills_dir(guest_home: str) -> str:
    return f"{guest_home}/.claude/skills"


def _prompt_path(guest_home: str) -> str:
    return f"{guest_home}/.bot-bottle-prompt.txt"

_RUNTIME = AgentProviderRuntime(
    template="claude",
    command="claude",
    image="bot-bottle-claude:latest",
    dockerfile=str(Path(__file__).resolve().parent / "Dockerfile"),
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
        guest_home: str,
        guest_env: dict[str, str] | None = None,
        auth_token: str = "",
        forward_host_credentials: bool = False,
        host_env: dict[str, str] | None = None,
        trusted_project_path: str = "",
        label: str = "",
        color: str = "",
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
        payload: dict[str, object] = {
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "bypassPermissionsModeAccepted": True,
            "projects": claude_projects,
        }
        if label:
            payload["name"] = label
        if color:
            payload["color"] = color
        claude_config.write_text(json.dumps(payload, indent=2) + "\n")
        claude_config.chmod(0o600)
        files = (
            AgentProvisionFile(claude_config, f"{guest_home}/.claude.json"),
        )
        egress_routes = (EgressRoute(
            host="api.anthropic.com",
            auth_scheme="Bearer" if auth_token else "",
            token_ref=auth_token,
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

    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy each named skill tree from `~/.claude/skills/<name>/`
        on the host into the guest's claude-code skills dir. No-op
        when the agent has no skills."""
        from ...backend.util import host_skill_dir

        agent = plan.spec.manifest.agents[plan.spec.agent_name]
        if not agent.skills:
            return
        skills_dir = _skills_dir(plan.guest_home)
        bottle.exec(f"mkdir -p {skills_dir}", user="root")
        for name in agent.skills:
            src = host_skill_dir(name)
            if not os.path.isdir(src):
                die(
                    f"skill {name!r} disappeared from host between "
                    f"validation and copy at {src}."
                )
            dst = f"{skills_dir}/{name}"
            info(f"copying skill {name} into {bottle.name}:{dst}")
            bottle.exec(f"rm -rf {dst} && mkdir -p {dst}", user="root")
            bottle.cp_in(f"{src}/.", f"{dst}/")
            bottle.exec(f"chown -R node:node {dst}", user="root")

    def provision_prompt(self, plan: "BottlePlan", bottle: "Bottle") -> str | None:
        """Copy the prompt file into the guest, fix ownership/mode.
        Returns the in-guest path iff the agent has a non-empty
        prompt (drives `--append-system-prompt-file`); the file is
        copied either way so the path always exists."""
        prompt_path = _prompt_path(plan.guest_home)
        bottle.cp_in(str(plan.prompt_file), prompt_path)  # type: ignore
        bottle.exec(
            f"chown node:node {prompt_path} && chmod 600 {prompt_path}",
            user="root",
        )
        agent = plan.spec.manifest.agents[plan.spec.agent_name]
        return prompt_path if agent.prompt else None

    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Apply the claude-side declarative provision steps from
        `plan.agent_provision` — today that's the `claude.json`
        trust-marker file. Hot-replace this with a richer flow as
        claude-code's harness shape evolves."""
        provision = plan.agent_provision
        for d in provision.dirs:
            path = shlex.quote(d.guest_path)
            _exec(bottle, f"mkdir -p {path}", f"could not create {d.guest_path}")
            _exec(
                bottle,
                f"chown {shlex.quote(d.owner)} {path}",
                f"could not chown {d.guest_path}",
            )
            _exec(
                bottle,
                f"chmod {shlex.quote(d.mode)} {path}",
                f"could not chmod {d.guest_path}",
            )
        for command in provision.pre_copy:
            _exec(bottle, shlex.join(command.argv), command.error)
        for f in provision.files:
            bottle.cp_in(str(f.host_path), f.guest_path)
            path = shlex.quote(f.guest_path)
            _exec(
                bottle,
                f"chown {shlex.quote(f.owner)} {path}",
                f"could not chown {f.guest_path}",
            )
            _exec(
                bottle,
                f"chmod {shlex.quote(f.mode)} {path}",
                f"could not chmod {f.guest_path}",
            )
        for command in provision.verify:
            _exec(bottle, shlex.join(command.argv), command.error)

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


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
