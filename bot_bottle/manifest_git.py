"""Git-related manifest dataclasses and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .manifest_util import ManifestError, as_json_object

# Shell-safe characters for git-gate repo names. Names are embedded in
# the generated entrypoint shell script (shlex.quote is the primary
# defence; this regex is belt-and-suspenders and documents intent).
_GIT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_KEY_PROVIDERS = {"static", "gitea"}


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string (was {type(value).__name__})")
    return value


def parse_git_upstream(url: str, label: str) -> tuple[str, str, str, str]:
    """Parse `ssh://user@host[:port]/path` into (user, host, port, path).
    Dies if `url` doesn't match the ssh:// shape v1 supports. Default
    port is 22 (matches OpenSSH)."""
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
    """Configuration for a repo's SSH key in git-gate.repos.

    `provider` is either `"static"` (a pre-existing key on the host) or
    `"gitea"` (automatic deploy-key lifecycle via the Gitea API).

    For `static`: `path` is the host-side absolute path to the SSH private key.

    For `gitea`: `forge_token_env` is the name of a host-side env var
    carrying the Gitea API token; the value is read at provision time,
    never stored on the plan. `api_url` is the forge's HTTP API root; if
    empty, it is derived from the upstream URL's host at provision time."""

    provider: str
    path: str = ""
    forge_token_env: str = ""
    api_url: str = ""


@dataclass(frozen=True)
class ManifestGitEntry:
    """One upstream the per-agent git-gate (PRD 0008) is allowed to
    talk to. `Upstream` is the real remote URL the agent would push to
    if there were no gate; the gate hosts a bare repo at /git/<Name>.git
    and `IdentityFile` is the SSH key the gate uses to push that repo
    upstream after gitleaks passes. The agent itself never holds the
    upstream credential.

    The Upstream URL is parsed once at construction and the pieces are
    stashed in the `Upstream*` fields so the git-gate render step
    doesn't have to re-parse.

    Manifest source: `git-gate.repos.<Name>` (PRD 0047/0048). A `key`
    block is required; `key.provider` is `"static"` or `"gitea"`. For
    `static`, `IdentityFile` is populated at parse time from `key.path`.
    For `gitea`, `IdentityFile` is populated at provision time."""

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
        """Parse one entry from `git-gate.repos.<repo_name>`.

        YAML keys: `url` (required), `key` (required object with
        `provider`, and provider-specific fields), `host_key` (optional).
        The repo_name becomes `Name`."""
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

    if provider == "gitea":
        for k in d:
            if k not in {"provider", "forge_token_env", "api_url"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label}.key has unknown key {k!r} "
                    f"for provider 'gitea'; allowed: provider, forge_token_env, api_url"
                )
        forge_token_env = d.get("forge_token_env")
        if not isinstance(forge_token_env, str) or not forge_token_env:
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key missing required "
                f"string field 'forge_token_env' for provider 'gitea'"
            )
        api_url_raw = d.get("api_url", "")
        if not isinstance(api_url_raw, str):
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.key 'api_url' must be a string"
            )
        return ManifestKeyConfig(
            provider=provider,
            forge_token_env=forge_token_env,
            api_url=api_url_raw,
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
    """Per-bottle `git config --global user.name` / `user.email`
    pair (issue #86). The agent's commits inside the bottle are
    attributed to this identity rather than the agent image's
    image-baked default (no user, or whatever the image dropped
    in). Either or both fields can be set independently.

    `from_dict` is forgiving on shape (a single missing field is
    fine — we just skip that config line at provisioning) but
    strict on types (string-or-die)."""

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


def parse_git_gate_config(
    bottle_name: str,
    raw: object,
) -> tuple[tuple[ManifestGitEntry, ...], ManifestGitUser]:
    d = as_json_object(raw, f"bottle '{bottle_name}' git-gate")
    for k in d:
        if k not in {"user", "repos"}:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate has unknown key {k!r}; "
                f"allowed: user, repos"
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
