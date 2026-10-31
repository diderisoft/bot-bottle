"""Agent manifest dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

from ..agent_provider import BUILTIN_HARNESSES
from .util import ManifestError, as_json_object
from .git import ManifestGitUser
from .schema import AGENT_MODEL_KEYS, is_valid_entity_name, reject_agent_bottle


# A harness version lands in overlay RUN lines, so it must be strict semver.
HARNESS_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.]*)?")

# Debian package names (policy 5.6.1) with optional `:arch` and `=version`.
# Anything else, above all a newline, is refused, since entries land in RUN.
APT_PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*(:[a-z0-9-]+)?(=[A-Za-z0-9.+:~-]+)?")


@dataclass(frozen=True)
class ManifestImageOverlay:
    """A thin layer on the pinned default agent image: a harness version, apt
    packages, and extra files, built `FROM` the verified base. A harness bump
    replaces the tested harness, so compatibility is the author's.

    `dockerfile` is the free-form alternative: a single-stage Dockerfile built
    `FROM ${BASE}` with its own directory as context. Exclusive with the other
    fields."""

    harness_version: str = ""
    apt: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    dockerfile: str = ""

    def is_empty(self) -> bool:
        return not (self.harness_version or self.apt or self.files or self.dockerfile)

    @classmethod
    def from_dict(cls, agent_name: str, raw: object) -> "ManifestImageOverlay":
        where = f"agent '{agent_name}' agent_provider.overlay"
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
    # Relative to the launch directory; it lands in the build context and a
    # COPY.
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
    """The agent process's provider. `harness` picks a built-in runtime;
    `dockerfile` optionally names a custom agent image. `auth_token` names the
    host var holding Claude's OAuth token, injected by a provider egress route
    while the agent gets a placeholder. `forward_host_credentials` forwards the
    host's Codex or Claude login token through egress.
    """

    harness: str = "claude"
    dockerfile: str = ""
    auth_token: str = ""
    forward_host_credentials: bool = False
    settings: dict[str, object] = field(default_factory=dict)
    overlay: ManifestImageOverlay = field(default_factory=ManifestImageOverlay)

    @property
    def builds_custom_image(self) -> bool:
        """The agent names its own `dockerfile`, so its image is always built
        from it."""
        return bool(self.dockerfile)

    @classmethod
    def from_dict(cls, agent_name: str, raw: object) -> "ManifestAgentProvider":
        # `agent_provider: pi` is short for `{harness: pi}`.
        if isinstance(raw, str):
            raw = {"harness": raw}
        d = as_json_object(raw, f"agent '{agent_name}' agent_provider")
        for k in d:
            if k not in {
                "harness",
                "dockerfile",
                "auth_token",
                "forward_host_credentials",
                "settings",
                "overlay",
            }:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider has unknown key {k!r}; "
                    "allowed: harness, dockerfile, auth_token, "
                    "forward_host_credentials, settings, overlay"
                )
        harness = d.get("harness", "claude")
        if not isinstance(harness, str) or not harness:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.harness must be a "
                f"non-empty string"
            )
        dockerfile = d.get("dockerfile", "")
        if not isinstance(dockerfile, str):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.dockerfile must be a "
                f"string (was {type(dockerfile).__name__})"
            )
        auth_token = d.get("auth_token", "")
        if not isinstance(auth_token, str):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.auth_token must be a "
                f"string (was {type(auth_token).__name__})"
            )
        if auth_token and harness not in BUILTIN_HARNESSES:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.auth_token is only "
                f"supported for built-in harnesses "
                f"({', '.join(sorted(BUILTIN_HARNESSES))})"
            )
        if auth_token and harness != "claude":
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.auth_token is only "
                f"supported for harness 'claude'"
            )
        forward_host_credentials = d.get("forward_host_credentials", False)
        if not isinstance(forward_host_credentials, bool):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.forward_host_credentials "
                f"must be a boolean (was {type(forward_host_credentials).__name__})"
            )
        if forward_host_credentials and harness not in BUILTIN_HARNESSES:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.forward_host_credentials "
                f"is only supported for built-in harnesses "
                f"({', '.join(sorted(BUILTIN_HARNESSES))})"
            )
        if forward_host_credentials and harness not in {"codex", "claude"}:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.forward_host_credentials "
                "is only supported for harnesses 'codex' and 'claude'"
            )
        if forward_host_credentials and auth_token:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.forward_host_credentials "
                "and auth_token both set; use one or the other"
            )
        settings = _parse_provider_settings(agent_name, harness, d.get("settings"))
        overlay = ManifestImageOverlay()
        if "overlay" in d:
            overlay = ManifestImageOverlay.from_dict(agent_name, d["overlay"])
        if not overlay.is_empty() and dockerfile:
            raise ManifestError(
                f"agent '{agent_name}' sets both agent_provider.overlay and "
                "agent_provider.dockerfile; an overlay builds on the published "
                "default image, so use one or the other (an overlay's own "
                "Dockerfile goes in overlay.dockerfile)"
            )
        if not overlay.is_empty() and harness not in BUILTIN_HARNESSES:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.overlay is only "
                f"supported for built-in harnesses "
                f"({', '.join(sorted(BUILTIN_HARNESSES))}); a custom harness "
                "builds its own image and has nothing to overlay"
            )
        return cls(
            harness=harness,
            dockerfile=dockerfile,
            auth_token=auth_token,
            forward_host_credentials=forward_host_credentials,
            settings=settings,
            overlay=overlay,
        )


@dataclass(frozen=True)
class ManifestAgent:
    skills: tuple[str, ...] = ()
    prompt: str = ""
    # The agent's git identity, overlaid per field on the bottle's. Agents may
    # set only `user`; `repos` carries credentials and stays bottle-only.
    git_user: ManifestGitUser = ManifestGitUser()
    # `remote.writable`: whether remote viewers may type.
    remote_writable: bool = False
    # What runs: the harness, its image and settings, and what it demands of
    # any bottle (its API route and credentials). Agents don't compose, so
    # this is the agent's alone (PRD 0098).
    agent_provider: ManifestAgentProvider = field(default_factory=ManifestAgentProvider)

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ManifestAgent":
        d = as_json_object(raw, f"agent '{name}'")
        if "bottle" in d:
            reject_agent_bottle(name, d["bottle"])
        unknown = set(d.keys()) - AGENT_MODEL_KEYS
        if unknown:
            allowed = ", ".join(sorted(AGENT_MODEL_KEYS))
            raise ManifestError(
                f"agent '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
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
                # Skill names become path segments and land in shell commands,
                # so they must be kebab-case like entity filenames.
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

        # Agents may declare only `git-gate.user`; `repos` is bottle-only.
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

        remote_writable = False
        remote_raw = d.get("remote")
        if remote_raw is not None:
            rd = as_json_object(remote_raw, f"agent '{name}' remote")
            for k in rd:
                if k != "writable":
                    raise ManifestError(
                        f"agent '{name}' remote has unknown key {k!r}; allowed: writable"
                    )
            writable = rd.get("writable", False)
            if not isinstance(writable, bool):
                raise ManifestError(
                    f"agent '{name}' remote.writable must be a boolean "
                    f"(was {type(writable).__name__})"
                )
            remote_writable = writable

        agent_provider = (
            ManifestAgentProvider.from_dict(name, d["agent_provider"])
            if "agent_provider" in d else ManifestAgentProvider()
        )

        return cls(skills=skills, prompt=prompt, git_user=git_user,
                   remote_writable=remote_writable, agent_provider=agent_provider)


def _parse_provider_settings(
    agent_name: str,
    harness: str,
    raw: object,
) -> dict[str, object]:
    if raw is None:
        return {}
    settings = as_json_object(raw, f"agent '{agent_name}' agent_provider.settings")

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
    if harness == "pi":
        allowed = common_allowed | pi_allowed
    elif harness in ("claude", "codex"):
        allowed = common_allowed
    elif harness not in BUILTIN_HARNESSES:
        return dict(settings)
    else:
        allowed = common_allowed

    for key in settings:
        if key not in allowed:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings has unknown "
                f"key {key!r}; allowed: {', '.join(sorted(allowed))}"
            )
    startup_args = settings.get("startup_args")
    if startup_args is not None:
        if not isinstance(startup_args, list):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.startup_args "
                f"must be an array of strings"
            )
        for i, arg in enumerate(startup_args):
            if not isinstance(arg, str) or not arg:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider.settings."
                    f"startup_args[{i}] must be a non-empty string"
                )
    if harness != "pi":
        return dict(settings)

    for key in ("provider", "base_url", "api", "api_key", "api_key_env"):
        value = settings.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.{key} must "
                "be a non-empty string"
            )
    max_tokens_field = settings.get("max_tokens_field")
    if max_tokens_field is not None and max_tokens_field not in (
        "max_tokens", "max_completion_tokens",
    ):
        raise ManifestError(
            f"agent '{agent_name}' agent_provider.settings.max_tokens_field "
            "must be 'max_tokens' or 'max_completion_tokens'"
        )
    if settings.get("api_key") is not None and settings.get("api_key_env") is not None:
        raise ManifestError(
            f"agent '{agent_name}' agent_provider.settings may set either "
            "api_key or api_key_env, not both"
        )
    models = settings.get("models")
    if models is not None:
        if not isinstance(models, list) or not models:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.models must "
                "be a non-empty array of strings"
            )
        for i, model in enumerate(models):
            if not isinstance(model, str) or not model:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider.settings.models[{i}] "
                    "must be a non-empty string"
                )
    for key in ("supports_developer_role", "supports_reasoning_effort"):
        value = settings.get(key)
        if value is not None and not isinstance(value, bool):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.{key} must "
                f"be a boolean (was {type(value).__name__})"
            )
    for key in ("context_window", "max_tokens"):
        value = settings.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.{key} must "
                f"be a positive integer (was {type(value).__name__})"
            )
    return dict(settings)
