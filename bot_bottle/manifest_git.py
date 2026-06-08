"""Git-related manifest dataclasses and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .manifest_util import ManifestError, as_json_object

# Shell-safe characters for git-gate repo names. Names are embedded in
# the generated entrypoint shell script (shlex.quote is the primary
# defence; this regex is belt-and-suspenders and documents intent).
_GIT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
class ManifestProvisionedKeyConfig:
    """Configuration for automatic deploy-key lifecycle management
    (PRD 0048). Used when a git-gate.repos entry opts out of a
    static identity file and instead wants a fresh SSH keypair
    generated at spin-up and revoked at teardown.

    `provider` names the contrib sub-package to load (e.g. `gitea`).
    `token_env` is the name of a host-side env var carrying the API
    token; the value is read at provision time, never stored on the
    plan. `api_url` is the forge's HTTP API root; if empty, it is
    derived from the upstream URL's host at provision time."""

    provider: str
    token_env: str
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

    Manifest source: `git-gate.repos.<Name>` (PRD 0047/0048). Exactly
    one of `identity` (static key path) or `provisioned_key` (automatic
    lifecycle) must be present. The internal field names are stable."""

    Name: str
    Upstream: str
    IdentityFile: str = ""
    KnownHostKey: str = ""
    ProvisionedKey: Optional[ManifestProvisionedKeyConfig] = None
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

        YAML keys: `url` (required), exactly one of `identity` or
        `provisioned_key` (required), `host_key` (optional).
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
            if k not in {"url", "identity", "provisioned_key", "host_key"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label} has unknown key {k!r}; "
                    f"allowed: url, identity, provisioned_key, host_key"
                )
        upstream = d.get("url")
        if not isinstance(upstream, str) or not upstream:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required string field 'url'"
            )

        has_identity = "identity" in d
        has_provisioned = "provisioned_key" in d
        if has_identity and has_provisioned:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} must set exactly one of "
                f"'identity' or 'provisioned_key'; got both."
            )
        if not has_identity and not has_provisioned:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} must set exactly one of "
                f"'identity' or 'provisioned_key'; got neither."
            )

        ident = ""
        provisioned_key: Optional[ManifestProvisionedKeyConfig] = None
        if has_identity:
            raw_ident = d.get("identity")
            if not isinstance(raw_ident, str) or not raw_ident:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label} 'identity' must be a non-empty string"
                )
            ident = raw_ident
        else:
            provisioned_key = _parse_provisioned_key_config(
                bottle_name, label, d["provisioned_key"]
            )

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
            IdentityFile=ident,
            KnownHostKey=khk,
            ProvisionedKey=provisioned_key,
            RemoteKey=host,
            UpstreamUser=user,
            UpstreamHost=host,
            UpstreamPort=port,
            UpstreamPath=path,
        )


def _parse_provisioned_key_config(
    bottle_name: str, label: str, raw: object
) -> ManifestProvisionedKeyConfig:
    d = as_json_object(raw, f"bottle '{bottle_name}' {label}.provisioned_key")
    for k in d:
        if k not in {"provider", "token_env", "api_url"}:
            raise ManifestError(
                f"bottle '{bottle_name}' {label}.provisioned_key has unknown key {k!r}; "
                f"allowed: provider, token_env, api_url"
            )
    provider = d.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.provisioned_key missing required "
            f"string field 'provider'"
        )
    token_env = d.get("token_env")
    if not isinstance(token_env, str) or not token_env:
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.provisioned_key missing required "
            f"string field 'token_env'"
        )
    api_url_raw = d.get("api_url", "")
    if not isinstance(api_url_raw, str):
        raise ManifestError(
            f"bottle '{bottle_name}' {label}.provisioned_key 'api_url' must be a string"
        )
    return ManifestProvisionedKeyConfig(
        provider=provider,
        token_env=token_env,
        api_url=api_url_raw,
    )


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
