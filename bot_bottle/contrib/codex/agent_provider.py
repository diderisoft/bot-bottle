"""Codex agent provider plugin.

The Codex-specific behavior previously inlined under
`agent_provider.agent_provision_plan` (config.toml trust marker,
chatgpt.com / api.openai.com egress routes, optional host-credential
forwarding with dummy-auth.json + verify), plus the `codex mcp add`
invocation that registers the supervise daemon in Codex's
~/.codex/config.toml."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ...agent_provider import (
    CODEX_HOST_CREDENTIAL_HOSTS,
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionDir,
    AgentProvisionCommand,
    HostDetection,
    AgentProvisionFile,
    AgentProvisionPlan,
    provider_startup_args,
)
from ...starter import GENERAL_PROMPT, starter_agent, starter_bottle
from .codex_auth import (
    codex_host_access_token,
    codex_login_location,
    write_codex_dummy_auth_file,
)
from ...egress import CODEX_HOST_CREDENTIAL_TOKEN_REF, EgressRoute
from ...lib.log import die, info


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_SUPERVISE_MCP_NAME = "supervise"
# App-layer identity token header (mirrors egress_addon / git_http_backend).
_IDENTITY_HEADER = "x-bot-bottle-identity"
_CODEX_CLI = "/home/node/.codex/packages/standalone/current/bin/codex"
_CODEX_CLI_PATH = (
    "/home/node/.local/bin:"
    "/home/node/.codex/packages/standalone/current/bin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _toml_basic_string(value: str) -> str:
    """Quote `value` as a TOML basic (double-quoted) string."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _supervise_mcp_config_toml(supervise_url: str, token: str) -> str:
    """Render the `[mcp_servers.supervise]` streamable-HTTP entry for
    Codex's `config.toml`.

    The Codex CLI has no `mcp add --header` flag; a static request
    header on an HTTP MCP server is only expressible via the
    `http_headers` config key (see `RawMcpServerConfig` /
    `McpServerTransportConfig::StreamableHttp`). We deliver the
    mandatory identity token (source_ip, token attribution) that way.
    Only Codex-supported streamable-HTTP keys (`url`, `http_headers`)
    are emitted."""
    lines = [
        "",
        f"[mcp_servers.{_SUPERVISE_MCP_NAME}]",
        f"url = {_toml_basic_string(supervise_url)}",
    ]
    if token:
        key = _toml_basic_string(_IDENTITY_HEADER)
        val = _toml_basic_string(token)
        lines.append(f"http_headers = {{ {key} = {val} }}")
    lines.append("")
    return "\n".join(lines)


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

    def detect_host(self, host_env: Mapping[str, str]) -> HostDetection:
        location = codex_login_location(host_env)
        return HostDetection(
            installed=shutil.which("codex", path=host_env.get("PATH")) is not None,
            logged_in=bool(location),
            login_location=location,
            login_command="codex login --device-auth",
        )

    def create_default_agent_manifest(self, detection: HostDetection) -> str:
        del detection
        return starter_agent("codex", GENERAL_PROMPT)

    def create_default_bottle_manifest(self, detection: HostDetection) -> str:
        return starter_bottle(
            {"template": "codex", "forward_host_credentials": True},
            "Codex, signed in with this host's ChatGPT device login "
            f"({detection.login_location or 'codex login --device-auth'}). "
            "The gateway injects the login token, so the agent never sees it.",
        )

    def harness_overlay_lines(self, version: str) -> tuple[str, ...] | None:
        # Mirrors the Dockerfile's standalone install. The base checks a
        # committed SHA256SUMS; for an arbitrary version the checksum list is
        # fetched from the same upstream release, which catches a corrupt or
        # swapped asset but not a compromised release.
        release = f"https://github.com/openai/codex/releases/download/rust-v{version}"
        return (
            "USER node",
            'RUN case "$(dpkg --print-architecture)" in '
            "amd64) codex_target=x86_64-unknown-linux-musl ;; "
            "arm64) codex_target=aarch64-unknown-linux-musl ;; "
            '*) echo "unsupported Codex architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; '
            "esac "
            '&& codex_asset="codex-package-${codex_target}.tar.gz" '
            f"&& curl -fsSL {release}/codex-package_SHA256SUMS -o /tmp/codex-package_SHA256SUMS "
            '&& codex_sha256="$(awk -v asset="${codex_asset}" \'$2 == asset { print $1 }\' /tmp/codex-package_SHA256SUMS)" '
            '&& test -n "${codex_sha256}" '
            f'&& curl -fsSL "{release}/${{codex_asset}}" -o "/tmp/${{codex_asset}}" '
            '&& echo "${codex_sha256}  /tmp/${codex_asset}" | sha256sum -c - '
            f'&& codex_release="/home/node/.codex/packages/standalone/releases/{version}-${{codex_target}}" '
            '&& mkdir -p "${codex_release}" '
            '&& tar -xzf "/tmp/${codex_asset}" -C "${codex_release}" '
            '&& ln -sfn bin/codex "${codex_release}/codex" '
            '&& ln -sfn "${codex_release}" /home/node/.codex/packages/standalone/current '
            '&& rm "/tmp/${codex_asset}" /tmp/codex-package_SHA256SUMS '
            f'&& test "$(codex --version)" = "codex-cli {version}"',
        )

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
        host_env: Mapping[str, str] | None = None,
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
            provisioned_env[CODEX_HOST_CREDENTIAL_TOKEN_REF] = (
                codex_host_access_token(host_env)
            )
            auth_file = state_dir / "codex-auth.json"
            write_codex_dummy_auth_file(auth_file, host_env)
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
        """Register the supervise daemon as a streamable-HTTP MCP
        server in Codex's user config (`~/.codex/config.toml`).

        We write the `[mcp_servers.supervise]` entry directly rather
        than shelling out to `codex mcp add`: the CLI's `add` has no
        way to attach a static request header, and the identity token
        (mandatory (source_ip, token) attribution) MUST ride on the
        MCP request as `http_headers`. Failure is FATAL when supervise
        is enabled — a silently-unregistered server leaves the agent
        with no supervise access and, under mandatory attribution, no
        way to recover from inside the bottle."""
        if plan.supervise_plan is None:
            return
        info(f"registering supervise MCP server in agent codex config → {supervise_url}")
        token = getattr(plan, "identity_token", "")
        block = _supervise_mcp_config_toml(supervise_url, token)
        auth_dir = plan.agent_provision.guest_env.get("CODEX_HOME") \
            or f"{plan.guest_home}/.codex"
        config_path = f"{auth_dir}/config.toml"
        # Append via base64 so the TOML payload never has to survive a
        # shell-quoting round trip. node owns the config file, so append
        # as node to preserve ownership/mode.
        payload = base64.b64encode(block.encode()).decode()
        script = (
            f"printf %s {shlex.quote(payload)} | base64 -d "
            f">> {shlex.quote(config_path)}"
        )
        r = bottle.exec(script, user="node")
        if r.returncode != 0:
            die(
                "agent provider provisioning: could not register supervise "
                f"MCP server in {config_path}: "
                f"{(r.stderr or r.stdout or '').strip()}"
            )

    def interactive_prompt(self, prompt: str) -> list[str] | None:
        # `codex <prompt>`: the interactive UI, with the prompt as its first message.
        return [prompt]

    def headless_prompt(self, prompt: str) -> list[str]:
        return [prompt]


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
