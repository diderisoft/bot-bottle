"""Codex agent provider plugin (PRD 0050, contrib).

The Codex-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (config.toml trust marker,
chatgpt.com / api.openai.com egress routes, optional host-credential
forwarding with dummy-auth.json + verify), plus the `codex mcp add`
invocation that registers the supervise daemon in Codex's
~/.codex/config.toml (PRD 0050)."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    CODEX_HOST_CREDENTIAL_HOSTS,
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionDir,
    AgentProvisionCommand,
    AgentProvisionFile,
    AgentProvisionPlan,
    provider_startup_args,
)
from .codex_auth import codex_host_access_token, write_codex_dummy_auth_file
from ...egress import CODEX_HOST_CREDENTIAL_TOKEN_REF, EgressRoute
from ...log import die, info, warn


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_SUPERVISE_MCP_NAME = "supervise"
_CODEX_CLI = "/home/node/.codex/packages/standalone/current/bin/codex"
_CODEX_CLI_PATH = (
    "/home/node/.local/bin:"
    "/home/node/.codex/packages/standalone/current/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _skills_dir(guest_home: str) -> str:
    # Codex agents still read skills from the claude-code convention
    # (~/.claude/skills/) — the bot-bottle-codex image follows the
    # same layout. If Codex grows native skill discovery later,
    # change here.
    return f"{guest_home}/.claude/skills"


def _prompt_path(guest_home: str) -> str:
    return f"{guest_home}/.bot-bottle-prompt.txt"


_RUNTIME = AgentProviderRuntime(
    template="codex",
    command=_CODEX_CLI,
    image="bot-bottle-codex:latest",
    prompt_mode="read_prompt_file",
    bypass_args=("--dangerously-bypass-approvals-and-sandbox",),
    resume_args=("resume", "--last"),
    smoke_test=(_CODEX_CLI, "--version"),
)


class CodexAgentProvider(AgentProvider):
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
        del auth_token, label, color
        resolved_guest_env = dict(guest_env or {})
        startup_args = provider_startup_args(provider_settings)
        guest_home = self.guest_home
        trusted_path = trusted_project_path or guest_home

        env_vars: dict[str, str] = {
            "CODEX_CA_CERTIFICATE": "/etc/ssl/certs/ca-certificates.crt",
        }
        auth_dir = resolved_guest_env.get("CODEX_HOME", f"{guest_home}/.codex")
        if forward_host_credentials:
            env_vars["CODEX_HOME"] = auth_dir

        dirs = [AgentProvisionDir(auth_dir)]
        files: list[AgentProvisionFile] = []
        pre_copy: list[AgentProvisionCommand] = []
        verify: list[AgentProvisionCommand] = []
        provisioned_env: dict[str, str] = {}

        config_path = f"{auth_dir}/config.toml"
        config_file = state_dir / "codex-config.toml"
        toml_path = trusted_path.replace("\\", "\\\\").replace('"', '\\"')
        config_file.write_text(
            f'[projects."{toml_path}"]\n'
            'trust_level = "trusted"\n'
            "\n"
            "[tui]\n"
            'status_line = ["model-with-reasoning"]\n'
            'terminal_title = ["spinner", "project"]\n'
            'theme = "ansi"\n'
        )
        config_file.chmod(0o600)
        files.append(AgentProvisionFile(config_file, config_path))

        egress_routes: list[EgressRoute] = []
        for host in CODEX_HOST_CREDENTIAL_HOSTS:
            egress_routes.append(EgressRoute(
                host=host,
                auth_scheme="Bearer" if forward_host_credentials else "",
                token_ref=CODEX_HOST_CREDENTIAL_TOKEN_REF if forward_host_credentials else "",
            ))

        if forward_host_credentials:
            _host_env = host_env or dict(os.environ)
            provisioned_env[CODEX_HOST_CREDENTIAL_TOKEN_REF] = (
                codex_host_access_token(_host_env)
            )
            auth_file = state_dir / "codex-auth.json"
            write_codex_dummy_auth_file(auth_file, _host_env)
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
                f"PATH={_CODEX_CLI_PATH}",
                _CODEX_CLI, "login", "status",
            ), (
                "codex host credentials: dummy auth was copied into the "
                "guest, but Codex did not accept it"
            )))

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
            dirs=tuple(dirs),
            files=tuple(files),
            pre_copy=tuple(pre_copy),
            verify=tuple(verify),
            egress_routes=tuple(egress_routes),
            provisioned_env=provisioned_env,
        )

    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy each named skill tree from `~/.claude/skills/<name>/`
        on the host into the guest. No-op when the agent has no
        skills."""
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
        Codex reads it via the agent's `Read and follow the
        instructions in <path>.` bootstrap (see `prompt_args`); the
        file is copied either way so the path always exists."""
        prompt_path = _prompt_path(plan.guest_home)
        bottle.cp_in(str(plan.prompt_file), prompt_path)  # type: ignore
        bottle.exec(
            f"chown node:node {prompt_path} && chmod 600 {prompt_path}",
            user="root",
        )
        agent = plan.manifest.agent
        return prompt_path if plan.agent_provision.has_prompt or agent.prompt else None

    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Apply the codex-side declarative provision steps from
        `plan.agent_provision`: the `~/.codex/` dir + config.toml
        trust marker, plus the dummy-auth.json drop + `codex login
        status` verify when host-credential forwarding is on."""
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
        """Run `codex mcp add` inside the agent guest to register the
        supervise daemon in Codex's user config (~/.codex/config.toml).

        Mirrors the Claude provider's `claude mcp add` flow — failure
        is logged but not fatal."""
        if plan.supervise_plan is None:
            return
        info(f"registering supervise MCP server in agent codex config → {supervise_url}")
        r = bottle.exec(
            f"{shlex.quote(_CODEX_CLI)} mcp add {_SUPERVISE_MCP_NAME} --url "
            f"{shlex.quote(supervise_url)}",
            user="node",
        )
        if r.returncode != 0:
            warn(
                f"`codex mcp add supervise` failed (exit {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
                f"register manually with: "
                f"codex mcp add supervise --url {shlex.quote(supervise_url)}"
            )

    def headless_prompt(self, prompt: str) -> list[str]:
        return [prompt]


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
