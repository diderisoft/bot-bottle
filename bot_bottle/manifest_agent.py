"""Agent configuration manifest dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .agent_provider import PROVIDER_TEMPLATES
from ._manifest_util import ManifestError, _as_json_object
from .manifest_git import GitUser
from .manifest_schema import AGENT_MODEL_KEYS


@dataclass(frozen=True)
class AgentProvider:
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

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "AgentProvider":
        d = _as_json_object(raw, f"bottle '{bottle_name}' agent_provider")
        for k in d:
            if k not in {"template", "dockerfile", "auth_token", "forward_host_credentials"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider has unknown key {k!r}; "
                    f"allowed: template, dockerfile, auth_token, forward_host_credentials"
                )
        template = d.get("template", "claude")
        if not isinstance(template, str) or not template:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.template must be a "
                f"non-empty string"
            )
        if template not in PROVIDER_TEMPLATES:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.template {template!r} "
                f"is not one of {', '.join(sorted(PROVIDER_TEMPLATES))}"
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
        if forward_host_credentials and template != "codex":
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                "is currently only supported for template 'codex'"
            )
        return cls(
            template=template,
            dockerfile=dockerfile,
            auth_token=auth_token,
            forward_host_credentials=forward_host_credentials,
        )


@dataclass(frozen=True)
class Agent:
    bottle: str
    skills: tuple[str, ...] = ()
    prompt: str = ""
    # Per-agent git identity (issue #94). Overlays the referenced
    # bottle's git-gate.user per-field at `Manifest.bottle_for`. Only
    # `user` is allowed at the agent level; `repos` stays bottle-only
    # because it carries credentials and host trust.
    git_user: GitUser = GitUser()

    @classmethod
    def from_dict(cls, name: str, raw: object, bottle_names: set[str]) -> "Agent":
        d = _as_json_object(raw, f"agent '{name}'")
        unknown = set(d.keys()) - AGENT_MODEL_KEYS
        if unknown:
            allowed = ", ".join(sorted(AGENT_MODEL_KEYS))
            raise ManifestError(
                f"agent '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
            )

        bottle = d.get("bottle")
        if not isinstance(bottle, str) or not bottle:
            raise ManifestError(f"agent '{name}' must declare a 'bottle' field naming a defined bottle")
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
                raise ManifestError(f"agent '{name}' skills must be an array (was {type(skills_raw).__name__})")
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
            raise ManifestError(f"agent '{name}' prompt must be a string (was {type(prompt_raw).__name__})")

        # git-gate: agents may declare only `git-gate.user` (name/email).
        # `git-gate.repos` is bottle-only — it carries credentials and host trust.
        git_user = GitUser()
        git_raw = d.get("git-gate")
        if git_raw is not None:
            gd = _as_json_object(git_raw, f"agent '{name}' git-gate")
            for k in gd.keys():
                if k != "user":
                    raise ManifestError(
                        f"agent '{name}' git-gate.{k} is not allowed at the "
                        f"agent level; only git-gate.user (name/email) may be "
                        f"set on an agent. git-gate.repos is bottle-only "
                        f"(it carries credentials and host trust)."
                    )
            if "user" in gd:
                git_user = GitUser.from_dict(name, gd["user"])

        return cls(bottle=bottle, skills=skills, prompt=prompt, git_user=git_user)
