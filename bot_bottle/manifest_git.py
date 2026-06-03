"""Git-related manifest dataclasses and helpers."""

from __future__ import annotations

from dataclasses import dataclass

from ._manifest_util import ManifestError, _as_json_object


def _opt_str(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string (was {type(value).__name__})")
    return value


def _parse_git_upstream(url: str, label: str) -> tuple[str, str, str, str]:
    """Parse `ssh://user@host[:port]/path` into (user, host, port, path).
    Dies if `url` doesn't match the ssh:// shape v1 supports. Default
    port is 22 (matches OpenSSH)."""
    if not url.startswith("ssh://"):
        raise ManifestError(f"{label} must be an ssh:// URL (was {url!r})")
    rest = url[len("ssh://"):]
    if "@" not in rest:
        raise ManifestError(f"{label} must include a user (e.g. ssh://git@host/path.git); was {url!r}")
    user, _, hostpart = rest.partition("@")
    if not user:
        raise ManifestError(f"{label} user is empty in {url!r}")
    if "/" not in hostpart:
        raise ManifestError(f"{label} must include a path (e.g. ssh://git@host/path.git); was {url!r}")
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


def _validate_unique_git_names(bottle_name: str, git: tuple[GitEntry, ...]) -> None:
    seen: dict[str, None] = {}
    for g in git:
        if g.Name in seen:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.repos has duplicate name '{g.Name}'; "
                f"each entry maps to a distinct bare repo on the gate."
            )
        seen[g.Name] = None


@dataclass(frozen=True)
class GitEntry:
    """One upstream the per-agent git-gate (PRD 0008) is allowed to
    talk to. `Upstream` is the real remote URL the agent would push to
    if there were no gate; the gate hosts a bare repo at /git/<Name>.git
    and `IdentityFile` is the SSH key the gate uses to push that repo
    upstream after gitleaks passes. The agent itself never holds the
    upstream credential.

    The Upstream URL is parsed once at construction and the pieces are
    stashed in the `Upstream*` fields so the git-gate render step
    doesn't have to re-parse.

    Manifest source: `git-gate.repos.<Name>` (PRD 0047). The YAML keys
    are `url`, `identity`, and `host_key`; the internal field names are
    stable across that rename."""

    Name: str
    Upstream: str
    IdentityFile: str
    KnownHostKey: str = ""
    RemoteKey: str = ""
    UpstreamUser: str = ""
    UpstreamHost: str = ""
    UpstreamPort: str = ""
    UpstreamPath: str = ""

    @classmethod
    def from_repos_entry(
        cls, bottle_name: str, repo_name: str, raw: object
    ) -> "GitEntry":
        """Parse one entry from `git-gate.repos.<repo_name>`.

        YAML keys: `url` (required), `identity` (required),
        `host_key` (optional). The repo_name becomes `Name`."""
        if not repo_name:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate.repos has an empty key"
            )
        label = f"git-gate.repos[{repo_name!r}]"
        d = _as_json_object(raw, f"bottle '{bottle_name}' {label}")
        for k in d:
            if k not in {"url", "identity", "host_key"}:
                raise ManifestError(
                    f"bottle '{bottle_name}' {label} has unknown key {k!r}; "
                    f"allowed: url, identity, host_key"
                )
        upstream = d.get("url")
        if not isinstance(upstream, str) or not upstream:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required string field 'url'"
            )
        ident = d.get("identity")
        if not isinstance(ident, str) or not ident:
            raise ManifestError(
                f"bottle '{bottle_name}' {label} missing required string field 'identity'"
            )
        khk = _opt_str(
            d.get("host_key"),
            f"bottle '{bottle_name}' {label} host_key",
        )
        user, host, port, path = _parse_git_upstream(
            upstream, f"bottle '{bottle_name}' {label} url"
        )
        return cls(
            Name=repo_name,
            Upstream=upstream,
            IdentityFile=ident,
            KnownHostKey=khk,
            RemoteKey=host,
            UpstreamUser=user,
            UpstreamHost=host,
            UpstreamPort=port,
            UpstreamPath=path,
        )


@dataclass(frozen=True)
class GitUser:
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
    def from_dict(cls, bottle_name: str, raw: object) -> "GitUser":
        d = _as_json_object(raw, f"bottle '{bottle_name}' git-gate.user")
        for k in d.keys():
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


def _parse_git_gate_config(
    bottle_name: str,
    raw: object,
) -> tuple[tuple[GitEntry, ...], GitUser]:
    d = _as_json_object(raw, f"bottle '{bottle_name}' git-gate")
    for k in d.keys():
        if k not in {"user", "repos"}:
            raise ManifestError(
                f"bottle '{bottle_name}' git-gate has unknown key {k!r}; "
                f"allowed: user, repos"
            )

    git_user = (
        GitUser.from_dict(bottle_name, d["user"])
        if "user" in d
        else GitUser()
    )

    git: tuple[GitEntry, ...] = ()
    repos_raw = d.get("repos")
    if repos_raw is not None:
        repos = _as_json_object(repos_raw, f"bottle '{bottle_name}' git-gate.repos")
        git = tuple(
            GitEntry.from_repos_entry(bottle_name, name, entry)
            for name, entry in repos.items()
        )
        _validate_unique_git_names(bottle_name, git)

    return git, git_user
