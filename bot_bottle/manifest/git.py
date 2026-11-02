"""Git manifest dataclasses and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..contract.forges import FORGE_KINDS
from ..contract.git_trailers import TRAILER_MODES
from .util import ManifestError, as_json_object

# Shell-safe repo names (they appear in generated scripts, also quoted).
_GIT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_KEY_PROVIDERS = {"static", *FORGE_KINDS}


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string (was {type(value).__name__})")
    return value


def parse_git_upstream(url: str, label: str) -> tuple[str, str, str, str]:
    """Parse `ssh://user@host[:port]/path` into (user, host, port, path); port
    defaults to 22. Raises for any other shape."""
    if not url.startswith("ssh://"):
        raise ManifestError(f"{label} must be an ssh:// URL (was {url!r})")
    rest = url[len("ssh://"):]
    if "@" not in rest:
        raise ManifestError(
            f"{label} must include a user (e.g. ssh://git@host/path.git); "
            f"was {url!r}"
        )
    user, _, hostpart = rest.partition("@")
    if not user:
        raise ManifestError(f"{label} user is empty in {url!r}")
    if "/" not in hostpart:
        raise ManifestError(
            f"{label} must include a path (e.g. ssh://git@host/path.git); "
            f"was {url!r}"
        )
    hostport, _, path = hostpart.partition("/")
    if not path:
        raise ManifestError(f"{label} path is empty in {url!r}")
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        if not port.isdigit():
            raise ManifestError(f"{label} port must be numeric in {url!r}")
    else:
        host = hostport
        port = "22"
    if not host:
        raise ManifestError(f"{label} host is empty in {url!r}")
    return (user, host, port, path)


def validate_unique_git_names(bottle_name: str, git: tuple[ManifestGitEntry, ...]) -> None:
    seen: dict[str, None] = {}
    for g in git:
        if g.Name in seen:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.repos has duplicate name '{g.Name}'; "
                f"each entry maps to a distinct bare repo on the gate."
            )
        seen[g.Name] = None


@dataclass(frozen=True)
class ManifestKeyConfig:
    """A repo's SSH key: `provider` `static` (`path`, a host key file) or a
    forge kind, `gitea` or `github` (a deploy key via the forge's API;
    `forge_token_env` names the token's host var, read at provision time;
    `api_url` defaults from the upstream host; `read_only` mints a key that
    can't push)."""

    provider: str
    path: str = ""
    forge_token_env: str = ""
    api_url: str = ""
    read_only: bool = False


@dataclass(frozen=True)
class ManifestGitEntry:
    """One upstream the git-gate may reach. `Upstream` is the real remote; the
    gate hosts `/git/<Name>.git` and pushes upstream with `IdentityFile` after
    the scans. The URL is parsed once into the `Upstream*` fields.

    From `git-gate.repos.<Name>`. `IdentityFile` comes from `key.path` for
    `static`, or at provision time for a forge kind."""

    Name: str
    Upstream: str
    Key: ManifestKeyConfig = ManifestKeyConfig(provider="")
    IdentityFile: str = ""
    KnownHostKey: str = ""
    RemoteKey: str = ""
    UpstreamUser: str = ""
    UpstreamHost: str = ""
    UpstreamPort: str = ""
    UpstreamPath: str = ""

    @classmethod
    def from_repos_entry(
        cls, bottle_name: str, repo_name: str, raw: object
    ) -> "ManifestGitEntry":
        """Parse `git-gate.repos.<repo_name>`: `url` and `key` required,
        `host_key` optional."""
        if not repo_name:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.repos has an empty key"
            )
        if not _GIT_NAME_RE.match(repo_name):
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.repos name {repo_name!r} is invalid; "
                f"allowed characters: A-Z a-z 0-9 . _ -"
            )
        label = f"git-gate.repos[{repo_name!r}]"
        d = as_json_object(raw, f"bottle '{bottle_name}' {label}")
        for k in d:
            if k not in {"url", "key", "host_key"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label} has unknown key {k!r}; "
                    f"allowed: url, key, host_key"
                )
        upstream = d.get("url")
        if not isinstance(upstream, str) or not upstream:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required string field 'url'"
            )

        if "key" not in d:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required 'key' block"
            )
        key_config = _parse_key_config(bottle_name, label, d["key"])

        ident = key_config.path if key_config.provider == "static" else ""

        khk = _opt_str(
            d.get("host_key"),
            f"bottle '{bottle_name}' {label} host_key",
        )
        user, host, port, path = parse_git_upstream(
            upstream, f"bottle '{bottle_name}' {label} url"
        )
        return cls(
            Name=repo_name,
            Upstream=upstream,
            Key=key_config,
            IdentityFile=ident,
            KnownHostKey=khk,
            RemoteKey=host,
            UpstreamUser=user,
            UpstreamHost=host,
            UpstreamPort=port,
            UpstreamPath=path,
        )


def _parse_key_config(
    bottle_name: str, label: str, raw: object
) -> ManifestKeyConfig:
    d = as_json_object(raw, f"bottle '{bottle_name}' {label}.key")
    provider = d.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.key missing required "
            f"string field 'provider'"
        )
    if provider not in _KEY_PROVIDERS:
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.key provider {provider!r} is unknown; "
            f"allowed: {', '.join(sorted(_KEY_PROVIDERS))}"
        )

    if provider in FORGE_KINDS:
        for k in d:
            if k not in {"provider", "forge_token_env", "api_url", "read_only"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label}.key has unknown key {k!r} "
                    f"for provider {provider!r}; allowed: provider, forge_token_env, "
                    f"api_url, read_only"
                )
        forge_token_env = d.get("forge_token_env")
        if not isinstance(forge_token_env, str) or not forge_token_env:
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key missing required "
                f"string field 'forge_token_env' for provider {provider!r}"
            )
        api_url_raw = d.get("api_url", "")
        if not isinstance(api_url_raw, str):
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key 'api_url' must be a string"
            )
        read_only = d.get("read_only", False)
        if not isinstance(read_only, bool):
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key 'read_only' must be true or false"
            )
        return ManifestKeyConfig(
            provider=provider,
            forge_token_env=forge_token_env,
            api_url=api_url_raw,
            read_only=read_only,
        )

    # provider == "static"
    for k in d:
        if k not in {"provider", "path"}:
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key has unknown key {k!r} "
                f"for provider 'static'; allowed: provider, path"
            )
    path = d.get("path")
    if not isinstance(path, str) or not path:
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.key missing required "
            f"string field 'path' for provider 'static'"
        )
    return ManifestKeyConfig(provider=provider, path=path)


@dataclass(frozen=True)
class ManifestGitUser:
    """The bottle's `git config --global` user.name/email; either may be set.
    Loose on shape, strict on types."""

    name: str = ""
    email: str = ""

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "ManifestGitUser":
        d = as_json_object(raw, f"bottle '{bottle_name}' git-gate.user")
        for k in d:
            if k not in {"name", "email"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' git-gate.user has unknown key {k!r}; "
                    f"allowed: name, email"
                )
        name = d.get("name", "")
        email = d.get("email", "")
        if not isinstance(name, str):
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.user.name must be a string "
                f"(was {type(name).__name__})"
            )
        if not isinstance(email, str):
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.user.email must be a string "
                f"(was {type(email).__name__})"
            )
        if not name and not email:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.user is set but neither "
                f"name nor email is non-empty; remove the block or "
                f"fill at least one field."
            )
        return cls(name=name, email=email)

    def is_empty(self) -> bool:
        return not self.name and not self.email


def parse_git_gate_trailers(bottle_name: str, raw: object) -> str:
    """`git-gate.trailers`: how git-gate checks pushed commits carry the
    bottle's `Bot-Bottle` trailer (`require`, `warn` or `off`); "" when
    unset, which is `require`."""
    d = as_json_object(raw, f"bottle '{bottle_name}' git-gate")
    if "trailers" not in d:
        return ""
    value = d["trailers"]
    if not isinstance(value, str) or value not in TRAILER_MODES:
        raise ManifestError(
            f"bottle '{bottle_name}' git-gate.trailers must be one of "
            f"{', '.join(TRAILER_MODES)} (was {value!r})"
        )
    return value


def parse_git_gate_config(
    bottle_name: str,
    raw: object,
) -> tuple[tuple[ManifestGitEntry, ...], ManifestGitUser]:
    d = as_json_object(raw, f"bottle '{bottle_name}' git-gate")
    for k in d:
        if k not in {"user", "repos", "trailers"}:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate has unknown key {k!r}; "
                f"allowed: user, repos, trailers"
            )

    git_user = (
        ManifestGitUser.from_dict(bottle_name, d["user"])
        if "user" in d
        else ManifestGitUser()
    )

    git: tuple[ManifestGitEntry, ...] = ()
    repos_raw = d.get("repos")
    if repos_raw is not None:
        repos = as_json_object(repos_raw, f"bottle '{bottle_name}' git-gate.repos")
        git = tuple(
            ManifestGitEntry.from_repos_entry(bottle_name, name, entry)
            for name, entry in repos.items()
        )
        validate_unique_git_names(bottle_name, git)

    return git, git_user
