"""Claude agent provider plugin (PRD 0050, contrib).

The Claude-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (claude.json trust marker,
api.anthropic.com egress route, OAuth-token placeholder), plus
the `claude mcp add` invocation that registers the supervise
gateway in claude-code's user config (PRD 0013)."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
    provider_startup_args,
)
from ...backend.docker import util as docker_mod
from ...egress import CLAUDE_HOST_CREDENTIAL_TOKEN_REF, EgressRoute
from ...log import die, info, warn
from .claude_auth import claude_host_access_token


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_SUPERVISE_MCP_NAME = "supervise"
# App-layer identity token header (mirrors egress_addon / git_http_backend).
_IDENTITY_HEADER = "x-bot-bottle-identity"


def _skills_dir(guest_home: str) -> str:
    return f"{guest_home}/.claude/skills"


def _prompt_path(guest_home: str) -> str:
    return f"{guest_home}/.bot-bottle-prompt.txt"


_STATUS_LINE_COLORS = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
}

_CLAUDE_THEME_COLORS = {
    "red": "redBright",
    "green": "greenBright",
    "yellow": "yellowBright",
    "blue": "blueBright",
    "magenta": "magentaBright",
}


def _status_line_script(label: str, color: str) -> str:
    if not label:
        return "#!/bin/sh\nprintf '\\n'\n"
    label_q = shlex.quote(label)
    if color and color in _STATUS_LINE_COLORS:
        return (
            "#!/bin/sh\n"
            f"printf '%b%s%b\\n' '{_STATUS_LINE_COLORS[color]}' {label_q} '\\033[0m'\n"
        )
    return f"#!/bin/sh\nprintf '%s\\n' {label_q}\n"


def _custom_theme_payload(color: str) -> dict[str, object] | None:
    theme_color = _CLAUDE_THEME_COLORS.get(color)
    if not theme_color:
        return None
    return {
        "name": f"Bot-bottle {color}",
        "base": "dark",
        "overrides": {
            "claude": f"ansi:{theme_color}",
        },
    }


_RUNTIME = AgentProviderRuntime(
    template="claude",
    command="claude",
    image="bot-bottle-claude:latest",
    prompt_mode="append_file",
    bypass_args=("--dangerously-skip-permissions",),
    resume_args=("--continue",),
    smoke_test=("claude", "--version"),
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
        instance_name: str,
        prompt_file: Path,
        guest_env: dict[str, str] | None = None,
        auth_token: str = "",
        forward_host_credentials: bool = False,
        host_env: dict[str, str] | None = None,
        trusted_project_path: str = "",
        label: str = "",
        color: str = "",
        provider_settings: dict[str, object] | None = None,
    ) -> AgentProvisionPlan:
        resolved_guest_env = dict(guest_env or {})
        startup_args = provider_startup_args(provider_settings)
        guest_home = self.guest_home
        trusted_path = trusted_project_path or guest_home

        env_vars: dict[str, str] = {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        dirs = (
            AgentProvisionDir(f"{guest_home}/.claude"),
            AgentProvisionDir(f"{guest_home}/.claude/themes"),
        )
        claude_config = state_dir / "claude.json"
        claude_projects = {guest_home: {"hasTrustDialogAccepted": True}}
        claude_projects[trusted_path] = {"hasTrustDialogAccepted": True}
        payload: dict[str, object] = {
            "hasCompletedOnboarding": True,
            "theme": "dark",
            "bypassPermissionsModeAccepted": True,
            "projects": claude_projects,
        }
        claude_config.write_text(json.dumps(payload, indent=2) + "\n")
        claude_config.chmod(0o600)
        files = [
            AgentProvisionFile(claude_config, f"{guest_home}/.claude.json"),
        ]

        claude_settings = state_dir / "claude-settings.json"
        claude_settings_payload: dict[str, object] = {}
        if label or color:
            statusline_script = state_dir / "claude-statusline.sh"
            statusline_script.write_text(_status_line_script(label, color))
            statusline_script.chmod(0o755)
            files.append(AgentProvisionFile(
                statusline_script,
                f"{guest_home}/.claude/statusline.sh",
                mode="755",
            ))
            claude_settings_payload["statusLine"] = {
                "type": "command",
                "command": "~/.claude/statusline.sh",
            }
        theme_payload = _custom_theme_payload(color)
        if theme_payload is not None:
            theme_name = f"bot-bottle-{docker_mod.slugify(label or color)}"
            theme_file = state_dir / f"{theme_name}.json"
            theme_file.write_text(json.dumps(theme_payload, indent=2) + "\n")
            theme_file.chmod(0o644)
            files.append(AgentProvisionFile(
                theme_file,
                f"{guest_home}/.claude/themes/{theme_name}.json",
            ))
            claude_settings_payload["theme"] = f"custom:{theme_name}"
        if claude_settings_payload:
            claude_settings.write_text(json.dumps(claude_settings_payload, indent=2) + "\n")
            claude_settings.chmod(0o600)
            files.append(AgentProvisionFile(
                claude_settings,
                f"{guest_home}/.claude/settings.json",
            ))
        provisioned_env: dict[str, str] = {}
        if forward_host_credentials:
            _host_env = host_env or dict(os.environ)
            provisioned_env[CLAUDE_HOST_CREDENTIAL_TOKEN_REF] = (
                claude_host_access_token(_host_env)
            )

        cred_token_ref = (
            CLAUDE_HOST_CREDENTIAL_TOKEN_REF if forward_host_credentials
            else auth_token
        )
        egress_routes = (EgressRoute(
            host="api.anthropic.com",
            auth_scheme="Bearer" if (auth_token or forward_host_credentials) else "",
            token_ref=cred_token_ref,
        ),)
        hidden_env_names: frozenset[str] = frozenset()
        if auth_token or forward_host_credentials:
            env_vars["CLAUDE_CODE_OAUTH_TOKEN"] = "egress-placeholder"
            hidden_env_names = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

        has_prompt = prompt_file.exists() and bool(prompt_file.read_text())
        return AgentProvisionPlan(
            template=_RUNTIME.template,
            command=_RUNTIME.command,
            prompt_mode=_RUNTIME.prompt_mode,
            image=_RUNTIME.image,
            dockerfile=dockerfile,
            guest_home=guest_home,
            instance_name=instance_name,
            prompt_file=prompt_file,
            env_vars=env_vars,
            guest_env=resolved_guest_env,
            has_prompt=has_prompt,
            startup_args=startup_args,
            dirs=dirs,
            files=tuple(files),
            egress_routes=egress_routes,
            hidden_env_names=hidden_env_names,
            provisioned_env=provisioned_env,
        )

    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy each named skill tree from `~/.claude/skills/<name>/`
        on the host into the guest's claude-code skills dir. No-op
        when the agent has no skills."""
        from ...backend.util import host_skill_dir

        agent = plan.manifest.agent
        if not agent.skills:
            return
        skills_dir = _skills_dir(plan.guest_home)
        bottle.exec(f"mkdir -p {shlex.quote(skills_dir)}", user="root")
        for name in agent.skills:
            src = host_skill_dir(name)
            if not os.path.isdir(src):
                die(
                    f"skill {name!r} disappeared from host between "
                    f"validation and copy at {src}."
                )
            dst = f"{skills_dir}/{name}"
            info(f"copying skill {name} into {bottle.name}:{dst}")
            # Defense in depth: skill names are validated kebab-case at
            # manifest load, but quote the path so a future unvalidated
            # field can't inject shell metacharacters here either.
            dst_q = shlex.quote(dst)
            bottle.exec(f"rm -rf {dst_q} && mkdir -p {dst_q}", user="root")
            bottle.cp_in(f"{src}/.", f"{dst}/")
            bottle.exec(f"chown -R node:node {dst_q}", user="root")

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
        agent = plan.manifest.agent
        return prompt_path if plan.agent_provision.has_prompt or agent.prompt else None

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
        supervise daemon in claude-code's user config (~/.claude.json).

        Failure is logged but not fatal — the bottle still works without
        the entry; the operator can register it manually."""
        if plan.supervise_plan is None:
            return
        info(f"registering supervise MCP server in agent claude config → {supervise_url}")
        # Deliver the identity token as an MCP request header — the supervise
        # daemon requires it (mandatory (source_ip, token) attribution).
        token = getattr(plan, "identity_token", "")
        header = (
            f" --header {shlex.quote(f'{_IDENTITY_HEADER}: {token}')}" if token else ""
        )
        r = bottle.exec(
            f"claude mcp add --scope user --transport http "
            f"{_SUPERVISE_MCP_NAME} {supervise_url}{header}",
            user="node",
        )
        if r.returncode != 0:
            manual_header = (
                f" --header {shlex.quote(f'{_IDENTITY_HEADER}: {token}')}" if token else ""
            )
            warn(
                f"`claude mcp add supervise` failed (exit {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
                f"register manually with: "
                f"claude mcp add --scope user --transport http "
                f"supervise {supervise_url}{manual_header}"
            )

    def headless_prompt(self, prompt: str) -> list[str]:
        return ["-p", prompt]


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
