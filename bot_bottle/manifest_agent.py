"""Agent configuration manifest dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .agent_provider import PROVIDER_TEMPLATES
from .manifest_util import ManifestError, as_json_object
from .manifest_git import ManifestGitUser
from .manifest_schema import AGENT_MODEL_KEYS


@dataclass(frozen=True)
class ManifestAgentProvider:
    """Provider/template for the agent process inside a bottle.

    `template` selects a built-in launch/runtime contract. `dockerfile`
    optionally points at a custom agent-image Dockerfile while leaving
    bot-bottle's sidecar infrastructure intact.

    `auth_token` names the host env var that holds the provider's OAuth
    token (Claude only). The provisioner injects a provider-owned egress
    route for api.anthropic.com that re-injects this token as the Bearer
    header, and sets a placeholder CLAUDE_CODE_OAUTH_TOKEN in the agent
    so the Claude Code CLI starts.

    `forward_host_credentials` forwards the host Codex auth token into
    the egress sidecar (Codex only).
    """

    template: str = "claude"
    dockerfile: str = ""
    auth_token: str = ""
    forward_host_credentials: bool = False
    settings: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "ManifestAgentProvider":
        d = as_json_object(raw, f"bottle '{bottle_name}' agent_provider")
        for k in d:
            if k not in {
                "template",
                "dockerfile",
                "auth_token",
                "forward_host_credentials",
                "settings",
            }:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider has unknown key {k!r}; "
                    "allowed: template, dockerfile, auth_token, "
                    "forward_host_credentials, settings"
                )
        template = d.get("template", "claude")
        if not isinstance(template, str) or not template:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.template must be a "
                f"non-empty string"
            )
        dockerfile = d.get("dockerfile", "")
        if not isinstance(dockerfile, str):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.dockerfile must be a "
                f"string (was {type(dockerfile).__name__})"
            )
        auth_token = d.get("auth_token", "")
        if not isinstance(auth_token, str):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.auth_token must be a "
                f"string (was {type(auth_token).__name__})"
            )
        if auth_token and template not in PROVIDER_TEMPLATES:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.auth_token is only "
                f"supported for built-in templates "
                f"({', '.join(sorted(PROVIDER_TEMPLATES))})"
            )
        if auth_token and template != "claude":
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.auth_token is only "
                f"supported for template 'claude'"
            )
        forward_host_credentials = d.get("forward_host_credentials", False)
        if not isinstance(forward_host_credentials, bool):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                f"must be a boolean (was {type(forward_host_credentials).__name__})"
            )
        if forward_host_credentials and template not in PROVIDER_TEMPLATES:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                f"is only supported for built-in templates "
                f"({', '.join(sorted(PROVIDER_TEMPLATES))})"
            )
        if forward_host_credentials and template != "codex":
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                "is currently only supported for template 'codex'"
            )
        settings = _parse_provider_settings(bottle_name, template, d.get("settings"))
        return cls(
            template=template,
            dockerfile=dockerfile,
            auth_token=auth_token,
            forward_host_credentials=forward_host_credentials,
            settings=settings,
        )


@dataclass(frozen=True)
class ManifestAgent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""
    # Per-agent git identity (issue #94). Overlays the referenced
    # bottle's git-gate.user per-field at `Manifest.bottle_for`. Only
    # `user` is allowed at the agent level; `repos` stays bottle-only
    # because it carries credentials and host trust.
    git_user: ManifestGitUser = ManifestGitUser()

    @classmethod
    def from_dict(cls, name: str, raw: object, bottle_names: set[str]) -> "ManifestAgent":
        d = as_json_object(raw, f"agent '{name}'")
        unknown = set(d.keys()) - AGENT_MODEL_KEYS
        if unknown:
            allowed = ", ".join(sorted(AGENT_MODEL_KEYS))
            raise ManifestError(
                f"agent '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
            )

        bottle = d.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            raise ManifestError(
                f"agent '{name}' must declare a 'bottle' field naming a "
                f"defined bottle"
            )
        if bottle not in bottle_names:
            available = ", ".join(sorted(bottle_names)) or "(none defined)"
            raise ManifestError(
                f"agent '{name}' references bottle '{bottle}', which is not defined. "
                f"Available: {available}"
            )

        skills: tuple[str, ...] = ()
        skills_raw = d.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, list):
                raise ManifestError(
                    f"agent '{name}' skills must be an array "
                    f"(was {type(skills_raw).__name__})"
                )
            collected: list[str] = []
            skills_list = cast(list[object], skills_raw)
            for i, skill in enumerate(skills_list):
                if not isinstance(skill, str):
                    raise ManifestError(
                        f"agent '{name}' skills[{i}] must be a string "
                        f"(was {type(skill).__name__})"
                    )
                collected.append(skill)
            skills = tuple(collected)

        prompt_raw = d.get("prompt")
        if prompt_raw is None:
            prompt = ""
        elif isinstance(prompt_raw, str):
            prompt = prompt_raw
        else:
            raise ManifestError(
                f"agent '{name}' prompt must be a string "
                f"(was {type(prompt_raw).__name__})"
            )

        # git-gate: agents may declare only `git-gate.user` (name/email).
        # `git-gate.repos` is bottle-only — it carries credentials and host trust.
        git_user = ManifestGitUser()
        git_raw = d.get("git-gate")
        if git_raw is not None:
            gd = as_json_object(git_raw, f"agent '{name}' git-gate")
            for k in gd:
                if k != "user":
                    raise ManifestError(
                        f"agent '{name}' git-gate.{k} is not allowed at the "
                        f"agent level; only git-gate.user (name/email) may be "
                        f"set on an agent. git-gate.repos is bottle-only "
                        f"(it carries credentials and host trust)."
                    )
            if "user" in gd:
                git_user = ManifestGitUser.from_dict(name, gd["user"])

        return cls(bottle=bottle, skills=skills, prompt=prompt, git_user=git_user)


def _parse_provider_settings(
    bottle_name: str,
    template: str,
    raw: object,
) -> dict[str, object]:
    if raw is None:
        return {}
    if template != "pi":
        raise ManifestError(
            f"bottle '{bottle_name}' agent_provider.settings is only "
            "supported for template 'pi'"
        )
    settings = as_json_object(raw, f"bottle '{bottle_name}' agent_provider.settings")
    allowed = {
        "base_url",
        "api",
        "api_key",
        "models",
        "supports_developer_role",
        "supports_reasoning_effort",
    }
    for key in settings:
        if key not in allowed:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings has unknown "
                f"key {key!r}; allowed: {', '.join(sorted(allowed))}"
            )
    for key in ("base_url", "api", "api_key"):
        value = settings.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.{key} must "
                "be a non-empty string"
            )
    models = settings.get("models")
    if models is not None:
        if not isinstance(models, list) or not models:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.models must "
                "be a non-empty array of strings"
            )
        for i, model in enumerate(models):
            if not isinstance(model, str) or not model:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider.settings.models[{i}] "
                    "must be a non-empty string"
                )
    for key in ("supports_developer_role", "supports_reasoning_effort"):
        value = settings.get(key)
        if value is not None and not isinstance(value, bool):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.{key} must "
                f"be a boolean (was {type(value).__name__})"
            )
    return dict(settings)
