"""Agent configuration manifest dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

from ..agent_provider import PROVIDER_TEMPLATES
from .util import ManifestError, as_json_object
from .git import ManifestGitUser
from .schema import AGENT_MODEL_KEYS, is_valid_entity_name


# A harness version is interpolated into overlay RUN lines, so it is held to a
# strict semver shape (optional prerelease) rather than shell-quoted ad hoc.
HARNESS_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.]*)?")

# Debian package names (policy 5.6.1), with an optional `:arch` and `=version`.
# Overlay apt entries land in a RUN line, so anything else (a newline above
# all, which would start a new Dockerfile instruction) is refused.
APT_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*(:[a-z0-9-]+)?(=[A-Za-z0-9.+:~-]+)?")


@dataclass(frozen=True)
class ManifestImageOverlay:
    """A small tweak layered on the pinned default agent image (PRD 0085).

    Rendered as a thin `FROM <pinned-default>` layer — a pinned
    harness version, extra apt packages, and extra files COPYed in — so the base
    stays the verified release image. A harness bump is the exception: it
    replaces the harness the release was tested with, so compatibility is the
    author's to own. This is the
    middle path between "pull the default as-is" and "bring your own image": a
    one-layer build, expressible without a `contrib` folder.

    `dockerfile` is the free-form alternative to the structured fields: a
    Dockerfile whose single stage is `FROM ${BASE}`, built with its own
    directory as the context. Mutually exclusive with the other fields."""

    harness_version: str = ""
    apt: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    dockerfile: str = ""

    def is_empty(self) -> bool:
        return not (self.harness_version or self.apt or self.files or self.dockerfile)

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "ManifestImageOverlay":
        where = f"bottle '{bottle_name}' agent_provider.overlay"
        d = as_json_object(raw, where)
        for k in d:
            if k not in {"harness_version", "apt", "files", "dockerfile"}:
                raise ManifestError(
                    f"{where} has unknown key {k!r}; allowed: "
                    "harness_version, apt, files, dockerfile"
                )
        harness = d.get("harness_version", "")
        if not isinstance(harness, str):
            raise ManifestError(
                f"{where}.harness_version must be a string "
                f"(was {type(harness).__name__})"
            )
        if harness and not HARNESS_VERSION_RE.fullmatch(harness):
            raise ManifestError(
                f"{where}.harness_version {harness!r} must be an exact "
                "version like 2.1.180 (or 0.146.0-alpha.1)"
            )
        apt = _overlay_str_list(where, "apt", d.get("apt", []))
        for pkg in apt:
            if not APT_PACKAGE_RE.fullmatch(pkg):
                raise ManifestError(
                    f"{where}.apt entry {pkg!r} is not a Debian package name "
                    "(optionally with :arch and =version)"
                )
        files = _overlay_str_list(where, "files", d.get("files", []))
        for f in files:
            _check_overlay_path(f"{where}.files entry", f)
        dockerfile = d.get("dockerfile", "")
        if not isinstance(dockerfile, str):
            raise ManifestError(
                f"{where}.dockerfile must be a string (was {type(dockerfile).__name__})"
            )
        if dockerfile:
            if harness or apt or files:
                raise ManifestError(
                    f"{where}.dockerfile cannot be combined with harness_version, "
                    "apt, or files; put those steps in the Dockerfile instead"
                )
            _check_overlay_path(f"{where}.dockerfile", dockerfile)
        return cls(harness_version=harness, apt=apt, files=files, dockerfile=dockerfile)


def _check_overlay_path(what: str, path: str) -> None:
    # Resolved against the launch directory; it lands in the build context
    # and (for `files`) in a COPY line.
    if path.startswith("/") or ".." in path.split("/"):
        raise ManifestError(
            f"{what} {path!r} must be a relative path inside the launch directory "
            "(no leading '/' or '..')"
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ManifestError(f"{what} {path!r} must not contain control characters")


def _overlay_str_list(where: str, key: str, raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ManifestError(f"{where}.{key} must be a list of non-empty strings")
    return tuple(cast("list[str]", raw))


@dataclass(frozen=True)
class ManifestAgentProvider:
    """Provider/template for the agent process inside a bottle.

    `template` selects a built-in launch/runtime contract. `dockerfile`
    optionally points at a custom agent-image Dockerfile while leaving
    bot-bottle's gateway infrastructure intact.

    `auth_token` names the host env var that holds the provider's OAuth
    token (Claude only). The provisioner injects a provider-owned egress
    route for api.anthropic.com that re-injects this token as the Bearer
    header, and sets a placeholder CLAUDE_CODE_OAUTH_TOKEN in the agent
    so the Claude Code CLI starts.

    `forward_host_credentials` forwards the host provider auth token into
    the egress sidecar (Codex and Claude). For Codex this reads
    `~/.codex/auth.json`; for Claude it reads `~/.claude/.credentials.json`.
    """

    template: str = "claude"
    dockerfile: str = ""
    auth_token: str = ""
    forward_host_credentials: bool = False
    settings: dict[str, object] = field(default_factory=dict)
    overlay: ManifestImageOverlay = field(default_factory=ManifestImageOverlay)

    @property
    def builds_custom_image(self) -> bool:
        """The bottle names its own `dockerfile`, so its image is built from
        that, never swapped for the published default (PRD 0085)."""
        return bool(self.dockerfile)

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
                "overlay",
            }:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider has unknown key {k!r}; "
                    "allowed: template, dockerfile, auth_token, "
                    "forward_host_credentials, settings, overlay"
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
        if forward_host_credentials and template not in {"codex", "claude"}:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                "is only supported for templates 'codex' and 'claude'"
            )
        if forward_host_credentials and auth_token:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.forward_host_credentials "
                "and auth_token both set; use one or the other"
            )
        settings = _parse_provider_settings(bottle_name, template, d.get("settings"))
        overlay = ManifestImageOverlay()
        if "overlay" in d:
            overlay = ManifestImageOverlay.from_dict(bottle_name, d["overlay"])
        if not overlay.is_empty() and dockerfile:
            raise ManifestError(
                f"bottle '{bottle_name}' sets both agent_provider.overlay and "
                "agent_provider.dockerfile; an overlay builds on the published "
                "default image, so use one or the other (an overlay's own "
                "Dockerfile goes in overlay.dockerfile)"
            )
        if not overlay.is_empty() and template not in PROVIDER_TEMPLATES:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.overlay is only "
                f"supported for built-in templates "
                f"({', '.join(sorted(PROVIDER_TEMPLATES))}); a custom template "
                "builds its own image and has nothing to overlay"
            )
        return cls(
            template=template,
            dockerfile=dockerfile,
            auth_token=auth_token,
            forward_host_credentials=forward_host_credentials,
            settings=settings,
            overlay=overlay,
        )


@dataclass(frozen=True)
class ManifestAgent:
    # Optional: when empty the operator selects bottles at launch time.
    bottle: str = ""
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

        bottle_raw = d.get("bottle")
        bottle = ""
        if bottle_raw is not None:
            if not isinstance(bottle_raw, str) or not bottle_raw:
                raise ManifestError(
                    f"agent '{name}' bottle must be a non-empty string when declared"
                )
            if bottle_raw not in bottle_names:
                available = ", ".join(sorted(bottle_names)) or "(none defined)"
                raise ManifestError(
                    f"agent '{name}' references bottle '{bottle_raw}', which is not defined. "
                    f"Available: {available}"
                )
            bottle = bottle_raw

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
                # Skill names become host/guest path segments and are
                # interpolated into provisioning shell commands, so they
                # must fit the same kebab-case convention as bottle/agent
                # filenames — rejecting anything that could break out of a
                # path segment or inject shell metacharacters.
                if not is_valid_entity_name(skill):
                    raise ManifestError(
                        f"agent '{name}' skills[{i}] {skill!r} is not a valid "
                        f"skill name; must match [a-z][a-z0-9-]*"
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
    settings = as_json_object(raw, f"bottle '{bottle_name}' agent_provider.settings")

    common_allowed = {"startup_args"}
    pi_allowed = {
        "provider",
        "base_url",
        "api",
        "api_key",
        "api_key_env",
        "models",
        "context_window",
        "max_tokens_field",
        "max_tokens",
        "supports_developer_role",
        "supports_reasoning_effort",
    }
    if template == "pi":
        allowed = common_allowed | pi_allowed
    elif template in ("claude", "codex"):
        allowed = common_allowed
    elif template not in PROVIDER_TEMPLATES:
        return dict(settings)
    else:
        allowed = common_allowed

    for key in settings:
        if key not in allowed:
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings has unknown "
                f"key {key!r}; allowed: {', '.join(sorted(allowed))}"
            )
    startup_args = settings.get("startup_args")
    if startup_args is not None:
        if not isinstance(startup_args, list):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.startup_args "
                f"must be an array of strings"
            )
        for i, arg in enumerate(startup_args):
            if not isinstance(arg, str) or not arg:
                raise ManifestError(
                    f"bottle '{bottle_name}' agent_provider.settings."
                    f"startup_args[{i}] must be a non-empty string"
                )
    if template != "pi":
        return dict(settings)

    for key in ("provider", "base_url", "api", "api_key", "api_key_env"):
        value = settings.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.{key} must "
                "be a non-empty string"
            )
    max_tokens_field = settings.get("max_tokens_field")
    if max_tokens_field is not None and max_tokens_field not in (
        "max_tokens", "max_completion_tokens",
    ):
        raise ManifestError(
            f"bottle '{bottle_name}' agent_provider.settings.max_tokens_field "
            "must be 'max_tokens' or 'max_completion_tokens'"
        )
    if settings.get("api_key") is not None and settings.get("api_key_env") is not None:
        raise ManifestError(
            f"bottle '{bottle_name}' agent_provider.settings may set either "
            "api_key or api_key_env, not both"
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
    for key in ("context_window", "max_tokens"):
        value = settings.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ManifestError(
                f"bottle '{bottle_name}' agent_provider.settings.{key} must "
                f"be a positive integer (was {type(value).__name__})"
            )
    return dict(settings)
