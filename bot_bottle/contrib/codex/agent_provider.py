"""Codex agent provider plugin (PRD 0050, contrib).

The Codex-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (config.toml trust marker,
chatgpt.com / api.openai.com egress routes, optional host-credential
forwarding with dummy-auth.json + verify), plus the `codex mcp add`
invocation that registers the supervise sidecar in Codex's
~/.codex/config.toml (PRD 0050)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    CODEX_HOST_CREDENTIAL_HOSTS,
    GUEST_HOME,
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionCommand,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from ...codex_auth import codex_host_access_token, write_codex_dummy_auth_file
from ...egress import CODEX_HOST_CREDENTIAL_TOKEN_REF, EgressRoute
from ...log import info, warn


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_REPO_ROOT = Path(__file__).resolve().parents[3]

_SUPERVISE_MCP_NAME = "supervise"

_RUNTIME = AgentProviderRuntime(
    template="codex",
    command="codex",
    image="bot-bottle-codex:latest",
    dockerfile=str(_REPO_ROOT / "Dockerfile.codex"),
    prompt_mode="read_prompt_file",
    bypass_args=("--dangerously-bypass-approvals-and-sandbox",),
    resume_args=("resume", "--last"),
    remote_control_args=(),
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
        guest_home: str = GUEST_HOME,
        guest_env: dict[str, str] | None = None,
        auth_token: str = "",
        forward_host_credentials: bool = False,
        host_env: dict[str, str] | None = None,
        trusted_project_path: str = "",
    ) -> AgentProvisionPlan:
        del auth_token  # Claude-only knob
        resolved_guest_env = dict(guest_env or {})
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
        )
        config_file.chmod(0o600)
        files.append(AgentProvisionFile(config_file, config_path))

        egress_routes: list[EgressRoute] = []
        for host in CODEX_HOST_CREDENTIAL_HOSTS:
            egress_routes.append(EgressRoute(
                host=host,
                auth_scheme="Bearer" if forward_host_credentials else "",
                token_ref=CODEX_HOST_CREDENTIAL_TOKEN_REF if forward_host_credentials else "",
                tls_passthrough=True,
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
                "codex", "login", "status",
            ), (
                "codex host credentials: dummy auth was copied into the "
                "guest, but Codex did not accept it"
            )))

        return AgentProvisionPlan(
            template=_RUNTIME.template,
            command=_RUNTIME.command,
            prompt_mode=_RUNTIME.prompt_mode,
            image=_RUNTIME.image,
            dockerfile=dockerfile,
            env_vars=env_vars,
            guest_env=resolved_guest_env,
            dirs=tuple(dirs),
            files=tuple(files),
            pre_copy=tuple(pre_copy),
            verify=tuple(verify),
            egress_routes=tuple(egress_routes),
            provisioned_env=provisioned_env,
        )

    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        """Run `codex mcp add` inside the agent guest to register the
        supervise sidecar in Codex's user config (~/.codex/config.toml).

        Mirrors the Claude provider's `claude mcp add` flow — failure
        is logged but not fatal."""
        if plan.supervise_plan is None:
            return
        info(f"registering supervise MCP server in agent codex config → {supervise_url}")
        r = bottle.exec(
            f"codex mcp add --transport http "
            f"{_SUPERVISE_MCP_NAME} {supervise_url}",
            user="node",
        )
        if r.returncode != 0:
            warn(
                f"`codex mcp add supervise` failed (exit {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()}. Inside the bottle, "
                f"register manually with: "
                f"codex mcp add --transport http supervise {supervise_url}"
            )
