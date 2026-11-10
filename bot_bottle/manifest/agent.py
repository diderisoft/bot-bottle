"""Agent manifest dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

from ..agent_provider import BUILTIN_HARNESSES
from .. import secret_ref
from .util import ManifestError, OldFormError, as_json_object
from .git import ManifestGitUser
from .schema import AGENT_KEYS, AGENT_MODEL_KEYS, is_valid_entity_name, reject_agent_bottle


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

    `dockerfile` is the free-form alternative: inline Dockerfile content
    (a multi-line string) for a single-stage build `FROM ${BASE}`. Exclusive
    with the other fields."""

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
            if "\n" not in dockerfile:
                raise ManifestError(
                    f"{where}.dockerfile must be inline Dockerfile content "
                    f"(a multi-line string); use the YAML block scalar:\n"
                    f"  overlay:\n"
                    f"    dockerfile: |\n"
                    f"      FROM ${{BASE}}\n"
                    f"      RUN ..."
                )
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
    `dockerfile` (written `dockerfile_path`) optionally names a custom agent
    image. `login` says how it signs in to its model API, mapped onto the
    fields the providers take: `login: host` forwards the host's Codex or
    Claude login (`forward_host_credentials`); `{oauth_token: secret:NAME}`
    is Claude's OAuth token (`auth_token`); `{api_key: secret:NAME}` an API
    key (`settings["api_key_env"]`). Each is injected by the provider's
    egress route while the agent gets a placeholder.
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
        _refuse_old_provider(agent_name, d)
        for k in d:
            if k not in {"harness", "dockerfile_path", "login", "settings", "overlay"}:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider has unknown key {k!r}; "
                    "allowed: harness, dockerfile_path, login, settings, overlay"
                )
        harness = d.get("harness", "claude")
        if not isinstance(harness, str) or not harness:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.harness must be a "
                f"non-empty string"
            )
        dockerfile = d.get("dockerfile_path", "")
        if not isinstance(dockerfile, str):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.dockerfile_path must be a "
                f"string (was {type(dockerfile).__name__})"
            )
        forward_host_credentials, auth_token, api_key_env = _parse_login(
            agent_name, d["login"]) if "login" in d else (False, "", "")
        if (forward_host_credentials or auth_token or api_key_env) \
                and harness not in BUILTIN_HARNESSES:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.login is only supported for built-in "
                f"harnesses ({', '.join(sorted(BUILTIN_HARNESSES))})")
        if forward_host_credentials and harness not in {"codex", "claude"}:
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.login: host is only supported for "
                "harnesses 'codex' and 'claude'")
        if auth_token and harness != "claude":
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.login.oauth_token is only supported "
                "for harness 'claude'")
        if api_key_env and harness not in ("claude", "pi"):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.login.api_key is only supported for "
                "harnesses 'claude' and 'pi'")
        settings = _parse_provider_settings(agent_name, harness, d.get("settings"))
        if api_key_env:
            if settings.get("api_key") is not None:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider sets both settings.api_key and "
                    "login.api_key; use one")
            settings["api_key_env"] = api_key_env
        overlay = ManifestImageOverlay()
        if "overlay" in d:
            overlay = ManifestImageOverlay.from_dict(agent_name, d["overlay"])
        if not overlay.is_empty() and dockerfile:
            raise ManifestError(
                f"agent '{agent_name}' sets both agent_provider.overlay and "
                "agent_provider.dockerfile_path; an overlay builds on the published "
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
    # `identity.git`: the agent's git identity, overlaid per field on the
    # bottle's `git_gate.user`.
    git_user: ManifestGitUser = ManifestGitUser()
    # `remote.writable`: whether remote viewers may type.
    remote_writable: bool = False
    # What runs: the harness, its image and settings, and what it demands of
    # any bottle (its API route and credentials). Agents don't compose, so
    # this is the agent's alone (PRD 0098).
    agent_provider: ManifestAgentProvider = field(default_factory=ManifestAgentProvider)
    # `identity.accounts: {<forge host>: <user>}`: which user connected on a
    # forge this agent pushes as; a forge it doesn't name uses its default
    # (ADR 0019).
    forge_users: tuple[tuple[str, str], ...] = ()

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
                    f"agent '{name}' skills must be a list "
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

        if "git-gate" in d:
            raise OldFormError(f"agent '{name}'", "`git-gate.user` is now `identity.git`")
        if "forge_users" in d:
            raise OldFormError(f"agent '{name}'", "`forge_users` is now `identity.accounts`")
        git_user, forge_users = _identity(name, d.get("identity"))
        # Only the bottle architect reads `model` (`suggest.architect`).
        if "model" in d and name != "bottle-architect" and name not in _MODEL_WARNED:
            _MODEL_WARNED.add(name)
            # pylint: disable-next=import-outside-toplevel
            from ..lib.log import warn
            warn(f"agent '{name}': model is ignored; the harness picks its model (for pi, "
                 "agent_provider.settings.models)")

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
                   remote_writable=remote_writable, agent_provider=agent_provider,
                   forge_users=forge_users)


# Old agent_provider keys and their new forms.
_OLD_PROVIDER_KEYS = {
    "dockerfile": "`agent_provider.dockerfile` (a path) is now "
                  "`agent_provider.dockerfile_path`",
    "forward_host_credentials": "`forward_host_credentials: true` is now `login: host`",
    "auth_token": "`auth_token: NAME` is now `login: { oauth_token: secret:NAME }`",
}


def _refuse_old_provider(agent: str, d: dict[str, object]) -> None:
    where = f"agent '{agent}' agent_provider"
    for key, change in _OLD_PROVIDER_KEYS.items():
        if key in d:
            raise OldFormError(where, change)
    settings = d.get("settings")
    if isinstance(settings, dict) and "api_key_env" in settings:
        raise OldFormError(where, "`settings.api_key_env: NAME` is now "
                                  "`login: { api_key: secret:NAME }`")


LOGIN_KINDS = ("oauth_token", "api_key")


def _parse_login(agent: str, raw: object) -> tuple[bool, str, str]:
    """`login` as (forward_host_credentials, auth_token, api_key_env):
    `host`, `{oauth_token: secret:NAME}` or `{api_key: secret:NAME}`."""
    where = f"agent '{agent}' agent_provider.login"
    if raw == "host":
        return True, "", ""
    if not isinstance(raw, dict) or len(cast("dict[str, object]", raw)) != 1 \
            or next(iter(cast("dict[str, object]", raw))) not in LOGIN_KINDS:
        raise ManifestError(f"{where} must be host, {{ oauth_token: secret:NAME }} or "
                            f"{{ api_key: secret:NAME }}")
    kind, value = next(iter(cast("dict[str, object]", raw).items()))
    if not isinstance(value, str) or secret_ref.kind(value) != secret_ref.STORED \
            or secret_ref.problem(value):
        raise ManifestError(f"{where}.{kind} takes secret:NAME, NAME in the secrets file")
    name = secret_ref.stored_name(value)
    return (False, name, "") if kind == "oauth_token" else (False, "", name)


def agent_from_frontmatter(name: str, fm: dict[str, object], body: str) -> "ManifestAgent":
    """Agent `name` from its file's frontmatter and body (the prompt)."""
    agent_dict: dict[str, object] = {k: v for k, v in fm.items() if k in AGENT_KEYS}
    agent_dict["skills"] = fm.get("skills", [])
    agent_dict["prompt"] = body.strip()
    return ManifestAgent.from_dict(name, agent_dict)


_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


_MODEL_WARNED: set[str] = set()


def _identity(agent: str, raw: object) -> tuple[ManifestGitUser, tuple[tuple[str, str], ...]]:
    """`identity: { git: { name, email }, accounts: { <forge host>: <user> } }`."""
    if raw is None:
        return ManifestGitUser(), ()
    d = as_json_object(raw, f"agent '{agent}' identity")
    for k in d:
        if k not in ("git", "accounts"):
            raise ManifestError(f"agent '{agent}' identity has unknown key {k!r}; allowed: "
                                f"git, accounts")
    git_user = (ManifestGitUser.from_dict(f"agent '{agent}' identity.git", d["git"])
                if "git" in d else ManifestGitUser())
    return git_user, _forge_users(agent, d.get("accounts"))


def _forge_users(agent: str, raw: object) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    d = as_json_object(raw, f"agent '{agent}' identity.accounts")
    out: list[tuple[str, str]] = []
    for host, user in d.items():
        if not _HOST_RE.match(host) or not isinstance(user, str) or not _USER_RE.match(user):
            raise ManifestError(f"agent '{agent}' identity.accounts maps a forge's host to a "
                                f"user connected on it (was {host!r}: {user!r})")
        out.append((host, user))
    return tuple(out)


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
    elif harness == "claude":
        # `api_key_env`: the host secret holding an Anthropic API key.
        allowed = common_allowed | {"api_key_env"}
    elif harness == "codex":
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
                f"must be a list of strings"
            )
        for i, arg in enumerate(startup_args):
            if not isinstance(arg, str) or not arg:
                raise ManifestError(
                    f"agent '{agent_name}' agent_provider.settings."
                    f"startup_args[{i}] must be a non-empty string"
                )
    if harness == "claude":
        key_env = settings.get("api_key_env")
        if key_env is not None and (not isinstance(key_env, str) or not key_env):
            raise ManifestError(
                f"agent '{agent_name}' agent_provider.settings.api_key_env "
                "must be a non-empty string"
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
                "be a non-empty list of strings"
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
