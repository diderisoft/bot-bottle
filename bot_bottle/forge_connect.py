"""`bot-bottle forge connect` (PRD 0103): check a forge token, pin the forge's
SSH host key, and write the forge into a bottle that others extend.

The forge is Gitea or GitHub (`forge_kinds`). The token goes to the host
secrets file; the bottle gets a `credentials:` entry naming it, a
`git-gate.forges` entry, and, for Gitea, read-only routes for the forge's
packages and release downloads. A host bottle's write is a version.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from .manifest.util import ManifestError

_PACKAGE_ROUTE: dict[str, object] = {
    "methods": ["GET"],
    "paths": [{"value": "/api/packages/"},
              {"type": "regex", "value": "^/[^/]+/[^/]+/releases/download/"}],
}


@dataclass(frozen=True)
class ForgeEntry:
    """What `forge connect` learned, ready to write."""

    key: str            # `git-gate.forges` key: the host, or <account>@<host>
    kind: str
    url: str
    ssh: str
    account: str
    host_key: str
    # GitHub through the bot-bottle GitHub App's user token: its slug.
    app: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    @property
    def secret_name(self) -> str:
        """The host secrets file's name for its token."""
        return re.sub(r"[^A-Z0-9]+", "_", self.key.upper()).strip("_") + "_TOKEN"

    @property
    def label(self) -> str:
        """Its credential's label in the bottle."""
        label = re.sub(r"[^a-z0-9]+", "-", self.key.lower()).strip("-")
        return label if label[:1].isalpha() else f"forge-{label}"


def default_key(url: str, account: str, existing: Mapping[str, object]) -> str:
    """The host, or `<account>@<host>` when the bottle already has the host for
    another account."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    taken = existing.get(host)
    if isinstance(taken, dict) and taken.get("account") not in ("", None, account):
        return f"{account}@{host}"
    return host


def existing_forges(path: Path) -> dict[str, object]:
    """The `git-gate.forges` a bottle file declares itself."""
    if not path.is_file():
        return {}
    fm, _ = parse_frontmatter(path.read_text())
    git = fm.get("git-gate")
    forges = git.get("forges") if isinstance(git, dict) else None
    return dict(forges) if isinstance(forges, dict) else {}


def write_forge(path: Path, entry: ForgeEntry) -> None:
    """Add (or replace) `entry` in the bottle at `path`, creating it if needed:
    its credential, its forge, and (Gitea) a route for its packages unless the
    bottle already routes its host."""
    if path.is_file():
        fm, body = parse_frontmatter(path.read_text())
    else:
        fm, body = {}, (
            f"The {entry.kind} forge at {entry.url}, written by `bot-bottle forge connect`.\n"
            f"Bottles that extend this one grant its repos in a line:\n\n"
            f"    git-gate:\n      repos: [{entry.key}/<owner>/<repo>[:write]]")

    credentials = _section(fm, "credentials")
    credentials[entry.label] = {"source": "secret", "secret": entry.secret_name}
    fm["credentials"] = credentials

    git = _section(fm, "git-gate")
    forges = _section(git, "forges")
    forge: dict[str, object] = {"kind": entry.kind, "url": entry.url, "ssh": entry.ssh,
                                "token": f"${entry.label}", "account": entry.account}
    if entry.host_key:
        forge["host_key"] = entry.host_key
    if entry.app:
        forge["app"] = entry.app
    forges[entry.key] = forge
    git["forges"] = forges
    fm["git-gate"] = git

    if entry.kind == "gitea":
        # GitHub's packages and release assets live on other hosts; a bottle
        # that needs them routes those itself.
        egress = _section(fm, "egress")
        routes = egress.get("routes")
        routes = list(routes) if isinstance(routes, list) else []
        if not any(isinstance(r, dict) and r.get("host") == entry.host for r in routes):
            routes.append({"host": entry.host, "inspect": {"matches": [dict(_PACKAGE_ROUTE)]}})
        egress["routes"] = routes
        fm["egress"] = egress

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{serialize_yaml_subset(fm)}---\n\n{body.strip()}\n")


def _section(d: dict[str, object], key: str) -> dict[str, object]:
    value = d.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{key} must be a mapping to add a forge to it")
    return dict(value)


def bottle_path(bottle: str, *, host_bottles: Path, cwd: Path) -> tuple[Path, bool]:
    """Where `bottle` lives, and whether it's a project bottle (`./name`)."""
    project = bottle.startswith("./")
    name = bottle[2:] if project else bottle
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ManifestError(f"bottle name {name!r}: lowercase letters, digits and -")
    base = cwd / ".bot-bottle" / "bottles" if project else host_bottles
    return base / f"{name}.md", project


Keyscan = Callable[[str, str], str]


def ssh_host_port(ssh: str) -> tuple[str, str]:
    """`ssh://user@host[:port]` -> (host, port)."""
    hostport = ssh[len("ssh://"):].split("@", 1)[-1]
    host, _, port = hostport.partition(":")
    return host, port or "22"


class ConnectError(Exception):
    """The forge couldn't be connected; the message says why."""


@dataclass(frozen=True)
class Connected:
    """A forge connected: where it was written and what the token can do."""

    key: str
    bottle: str
    path: Path
    account: str
    admin_repos: int
    repos: int
    ssh: str
    host_key: str
    secret_name: str
    warnings: tuple[str, ...] = ()


def normalize_url(url: str) -> str:
    url = url.strip()
    return (url if "://" in url else f"https://{url}").rstrip("/")


def connect(url: str, token: str, *, bottle: str = "", name: str = "", ssh: str = "",
            cwd: Path, host_bottles: Path, kind: str = "") -> Connected:
    """`bot-bottle forge connect`, for the CLI and the console alike: check the
    forge is a kind bot-bottle connects (Gitea, GitHub) and the token works,
    pin the forge's SSH host key on first use, keep the token in the host
    secrets file, and write the forge into `bottle` (default: named after its
    kind) as a new version. `kind` skips recognizing the forge again. Raises
    ConnectError saying what's wrong. The token is never logged or returned."""
    # pylint: disable=import-outside-toplevel
    from .approvals import trust_project_bottle
    from .contract.forges import unsupported_forge
    from .forge_kinds import ForgeApiError, detect, forge_api
    from .git_gate.host_key import fetch_host_key
    from .manifest.versions import record_or_warn
    from .paths import host_secrets_path
    from .secret_source import write_secret

    url = normalize_url(url)
    if not url.startswith("https://") and not url.startswith("http://"):
        raise ConnectError(f"{url} isn't an http(s) URL")
    if ssh and not ssh.startswith("ssh://"):
        raise ConnectError("the SSH address is ssh://user@host[:port]")
    if not token.strip():
        raise ConnectError("no token given")
    kind = kind or detect(url)
    if not kind:
        raise ConnectError(unsupported_forge(urlparse(url).hostname or url))
    api = forge_api(kind)
    try:
        path, project = bottle_path(bottle or kind, host_bottles=host_bottles, cwd=cwd)
    except ManifestError as e:
        raise ConnectError(str(e)) from e
    try:
        check = api.check_token(url, token.strip())
    except ForgeApiError as e:
        raise ConnectError(f"the token didn't work: {e}; it needs {api.TOKEN_HELP}") from e
    warnings: list[str] = []
    if not check.admin_repos:
        warnings.append(f"{check.account} can't add a deploy key anywhere yet; give it "
                        f"admin on the repos bottles will grant")
    host = urlparse(url).hostname or ""
    ssh = ssh.rstrip("/") or check.ssh or f"ssh://git@{host}"
    try:
        host_key = fetch_host_key(*ssh_host_port(ssh))
    except RuntimeError as e:
        # Without SSH, git-gate can't reach the forge's repos at all.
        raise ConnectError(f"{e}; give an SSH address this host can reach, "
                           f"e.g. ssh://git@<ip>:<port>") from e
    key = name or default_key(url, check.account, existing_forges(path))
    entry = ForgeEntry(key=key, kind=kind, url=url, ssh=ssh, account=check.account,
                       host_key=host_key)
    write_secret(host_secrets_path(), entry.secret_name, token.strip())
    try:
        write_forge(path, entry)
    except ManifestError as e:
        raise ConnectError(f"{path}: {e}") from e
    if project:
        trust_project_bottle(cwd, path.stem)
    else:
        record_or_warn(f"forge connect: {key} in {path.stem}")
    return Connected(key=key, bottle=bottle or kind, path=path, account=check.account,
                     admin_repos=check.admin_repos, repos=check.repos, ssh=ssh,
                     host_key=host_key, secret_name=entry.secret_name,
                     warnings=tuple(warnings))


__all__ = ["ConnectError", "Connected", "ForgeEntry", "bottle_path", "connect", "default_key",
           "existing_forges", "normalize_url", "ssh_host_port", "write_forge"]
