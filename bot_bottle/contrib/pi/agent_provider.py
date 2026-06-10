"""Pi agent provider plugin (PRD 0058, contrib).

Pi uses ~/.pi/agent/models.json for custom provider/model settings.
This provider writes an Ollama-compatible default configuration and
lets bottles override the model endpoint and model ids via
agent_provider.settings.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ...agent_provider import (
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from ...egress import EgressRoute
from ...log import die, info


if TYPE_CHECKING:
    from ...backend import Bottle, BottlePlan


_DEFAULT_BASE_URL = "http://ollama:11434/v1"
_DEFAULT_MODEL = "qwen2.5-coder:7b"
_DEFAULT_PROVIDER_NAME = "ollama"
_DEFAULT_CONTEXT_WINDOW = 4096
_DEFAULT_MAX_TOKENS = 1024


def _skills_dir(guest_home: str) -> str:
    return f"{guest_home}/.pi/agent/skills"


def _prompt_path(guest_home: str) -> str:
    return f"{guest_home}/.bot-bottle-prompt.txt"


def _append_system_path(guest_home: str) -> str:
    return f"{guest_home}/.pi/agent/APPEND_SYSTEM.md"


def _models_path(guest_home: str) -> str:
    return f"{guest_home}/.pi/agent/models.json"


def _runtime_state_repair_script(guest_home: str) -> str:
    home = shlex.quote(guest_home)
    pi_home = shlex.quote(f"{guest_home}/.pi")
    context_sessions = shlex.quote(f"{guest_home}/.pi/context-mode/sessions")
    return (
        f"mkdir -p {context_sessions} /tmp/pi-subagents-uid-1000 && "
        f"chown node:node {home} && "
        f"chown -R node:node {pi_home} /tmp && "
        "chmod -R u+rwX /tmp && "
        f"chmod 755 {home} && "
        "chown root:root /tmp /var/tmp && "
        "chmod 1777 /tmp /var/tmp"
    )


def _settings_value(
    settings: dict[str, object],
    key: str,
    default: object,
) -> object:
    value = settings.get(key)
    return default if value is None else value


def _pi_models_json(
    settings: dict[str, object],
) -> tuple[dict[str, object], str, str, list[str], str]:
    provider_name = str(
        _settings_value(settings, "provider", _DEFAULT_PROVIDER_NAME)
    )
    base_url = str(_settings_value(settings, "base_url", _DEFAULT_BASE_URL))
    api = str(_settings_value(settings, "api", "openai-completions"))
    api_key = settings.get("api_key")
    api_key_env = str(settings.get("api_key_env", ""))
    models_raw = _settings_value(settings, "models", [_DEFAULT_MODEL])
    models = [str(model) for model in models_raw]  # type: ignore[union-attr]
    supports_developer_role = bool(
        _settings_value(settings, "supports_developer_role", False)
    )
    supports_reasoning_effort = bool(
        _settings_value(settings, "supports_reasoning_effort", False)
    )
    max_tokens_field = str(
        _settings_value(settings, "max_tokens_field", "max_tokens")
    )
    context_window = int(_settings_value(
        settings, "context_window", _DEFAULT_CONTEXT_WINDOW,
    ))
    max_tokens = int(_settings_value(
        settings, "max_tokens", _DEFAULT_MAX_TOKENS,
    ))
    input_context_window = max(1, context_window - max_tokens)
    provider: dict[str, object] = {
        "baseUrl": base_url,
        "api": api,
        "compat": {
            "supportsDeveloperRole": supports_developer_role,
            "supportsReasoningEffort": supports_reasoning_effort,
            "maxTokensField": max_tokens_field,
        },
        "models": [
            {
                "id": model,
                "name": model,
                "contextWindow": input_context_window,
                "maxTokens": max_tokens,
            }
            for model in models
        ],
    }
    if api_key is not None:
        provider["apiKey"] = str(api_key)
    elif api_key_env:
        provider["apiKey"] = "egress-placeholder"
    elif provider_name == _DEFAULT_PROVIDER_NAME:
        provider["apiKey"] = "ollama"
    payload: dict[str, object] = {
        "providers": {
            provider_name: provider,
        }
    }
    return payload, base_url, api_key_env, models, provider_name


def _route_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        die(
            "agent provider provisioning: pi settings base_url must be an "
            f"absolute URL (was {base_url!r})"
        )
    return parsed.hostname


_RUNTIME = AgentProviderRuntime(
    template="pi",
    command="pi",
    image="bot-bottle-pi:latest",
    prompt_mode="append_system_prompt",
    bypass_args=(),
    resume_args=(),
    remote_control_args=(),
)


class PiAgentProvider(AgentProvider):
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
        del auth_token, forward_host_credentials, host_env, trusted_project_path
        del label, color
        resolved_guest_env = dict(guest_env or {})
        guest_home = self.guest_home
        settings = dict(provider_settings or {})

        models_payload, base_url, api_key_env, models, provider_name = (
            _pi_models_json(settings)
        )
        models_file = state_dir / "pi-models.json"
        models_file.write_text(json.dumps(models_payload, indent=2) + "\n")
        models_file.chmod(0o600)

        has_prompt = prompt_file.exists() and bool(prompt_file.read_text())
        auth_scheme = "Bearer" if api_key_env else ""
        return AgentProvisionPlan(
            template=_RUNTIME.template,
            command=_RUNTIME.command,
            prompt_mode=_RUNTIME.prompt_mode,
            image=_RUNTIME.image,
            dockerfile=dockerfile,
            guest_home=guest_home,
            instance_name=instance_name,
            prompt_file=prompt_file,
            guest_env=resolved_guest_env,
            has_prompt=has_prompt,
            startup_args=(
                "--models",
                ",".join(f"{provider_name}/{model}" for model in models),
            ),
            dirs=(AgentProvisionDir(f"{guest_home}/.pi/agent"),),
            files=(AgentProvisionFile(models_file, _models_path(guest_home)),),
            egress_routes=(EgressRoute(
                host=_route_host(base_url),
                auth_scheme=auth_scheme,
                token_ref=api_key_env,
            ),),
        )

    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
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
        prompt_path = _prompt_path(plan.guest_home)
        append_system_path = _append_system_path(plan.guest_home)
        bottle.cp_in(str(plan.prompt_file), prompt_path)  # type: ignore
        bottle.exec(
            f"mkdir -p {shlex.quote(plan.guest_home)}/.pi/agent && "
            f"cp {shlex.quote(prompt_path)} {shlex.quote(append_system_path)} && "
            f"chown node:node {shlex.quote(prompt_path)} "
            f"{shlex.quote(append_system_path)} && "
            f"chmod 600 {shlex.quote(prompt_path)} "
            f"{shlex.quote(append_system_path)}",
            user="root",
        )
        # Pi's `--append-system-prompt` takes literal text, not a file path.
        # Use its documented APPEND_SYSTEM.md discovery path instead.
        return None

    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        provision = plan.agent_provision
        _exec(
            bottle,
            _runtime_state_repair_script(plan.guest_home),
            "could not prepare pi runtime state",
        )
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

    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        del plan, bottle, supervise_url


def _exec(bottle: "Bottle", script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
