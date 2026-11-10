"""The forges this host has connected (ADR 0019): `~/.bot-bottle/forges.md`.

A bottle describes a forge; the host connects it. This file holds what this
machine connected, keyed by the forge's host, the same key a repo grant's
prefix names:

  ---
  gitea.example.com:
    kind: gitea
    url: https://gitea.example.com
    ssh: ssh://git@10.0.4.2:30009       # how this machine reaches it
    host_key: ssh-ed25519 AAAA...       # pinned when connected
    users:
      alice:
        token: secret:GITEA_EXAMPLE_COM_TOKEN   # a name in ~/.bot-bottle/secrets
        default: true
      codex:
        token: secret:CODEX_GITEA_EXAMPLE_COM_TOKEN
  github.com:
    kind: github
    url: https://github.com
    ssh: ssh://git@github.com
    users:
      alice: { token: secret:GITHUB_COM_TOKEN, app: bot-bottle, transport: https }
  ---

Only `bot-bottle forge connect` writes it (a token as `secret:NAME`; the old
`$NAME` still reads, so a host's file never stops working). A user's name is what the forge
says the token belongs to. A launch uses these, never a bottle's own
description of the forge, so a token goes only where its host connected it.

`connections()` is the host's connection source; a launch asks it, not the
file, so connections could come from elsewhere without bottles changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from . import secret_ref
from .contract.forges import FORGE_KINDS
from .contract.yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset
from .manifest.util import ManifestError

FORGES_FILE = "forges.md"
TRANSPORTS = ("https", "deploy-key")
_FORGE_KEYS = {"kind", "url", "ssh", "host_key", "users"}
_USER_KEYS = {"token", "default", "app", "transport"}
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ForgeConnectionError(ManifestError):
    """`forges.md` is malformed, or a grant names what isn't connected. A
    manifest error: a bottle that needs it doesn't load."""


@dataclass(frozen=True)
class ForgeUser:
    """One account connected on a forge: whose token, kept as `token_secret`
    in the host secrets file. `transport` is GitHub's (ADR 0014): how git-gate
    reaches repos as this user. `app`: github.com through the bot-bottle
    GitHub App (ADR 0012), its slug."""

    name: str
    token_secret: str
    default: bool = False
    app: str = ""
    transport: str = ""


@dataclass(frozen=True)
class ForgeConnection:
    """One forge as this machine connected it."""

    host: str
    kind: str
    url: str
    ssh: str
    host_key: str = ""
    users: tuple[ForgeUser, ...] = field(default_factory=tuple)

    def user(self, name: str = "") -> ForgeUser:
        """`name`, else the default user, else the only one."""
        if name:
            for user in self.users:
                if user.name == name:
                    return user
            raise ForgeConnectionError(
                f"{name} isn't connected on {self.host} (connected: "
                f"{', '.join(u.name for u in self.users) or 'nobody'}); connect it with "
                f"`bot-bottle forge connect {self.url}` and that user's token")
        defaults = [u for u in self.users if u.default]
        if defaults:
            return defaults[0]
        if len(self.users) == 1:
            return self.users[0]
        raise ForgeConnectionError(
            f"{self.host} has {len(self.users)} users connected and none is the default; "
            f"pick one for the agent (`identity: {{ accounts: {{{self.host}: <user>}} }}`) "
            f"or connect one again with `--default`")

    def snapshot(self, user: ForgeUser) -> dict[str, str]:
        """What trusting a bottle that uses this forge records: where pushes go,
        and as whom (ADR 0019)."""
        return {"kind": self.kind, "url": self.url, "ssh": self.ssh,
                "host_key": self.host_key, "user": user.name,
                "transport": user.transport}


def forges_path(root: Path | None = None) -> Path:
    # pylint: disable-next=import-outside-toplevel
    from .paths import bot_bottle_root
    return (root or bot_bottle_root()) / FORGES_FILE


def default_ssh(url: str) -> str:
    return f"ssh://git@{urlparse(url).hostname or ''}"


def host_of(url: str) -> str:
    """A forge's key from its URL: the host, with a port if it has one."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return f"{host}:{parsed.port}" if parsed.port else host


def _str(d: dict[str, object], key: str, where: str) -> str:
    value = d.get(key, "")
    if not isinstance(value, str):
        raise ForgeConnectionError(f"{where}.{key} must be a string")
    return value


def _user(host: str, kind: str, name: str, raw: object) -> ForgeUser:
    where = f"{FORGES_FILE}: {host}.users.{name}"
    if not _USER_RE.match(name):
        raise ForgeConnectionError(f"{where}: a user is the forge's username")
    if not isinstance(raw, dict):
        raise ForgeConnectionError(f"{where} must be a mapping")
    unknown = set(raw) - _USER_KEYS
    if unknown:
        raise ForgeConnectionError(f"{where} has unknown key {sorted(unknown)[0]!r}")
    token = _str(raw, "token", where)
    # `secret:NAME`, or the `$NAME` connections written before it.
    secret = token[1:] if token.startswith("$") else secret_ref.stored_name(token)
    if not secret_ref.is_name(secret):
        raise ForgeConnectionError(f"{where}.token names a host secret, as secret:NAME")
    transport = _str(raw, "transport", where)
    allowed = TRANSPORTS if kind == "github" else ("", "deploy-key")
    if transport and transport not in allowed:
        named = " or ".join(a for a in allowed if a)
        raise ForgeConnectionError(f"{where}.transport must be {named}")
    app = _str(raw, "app", where)
    if app and kind != "github":
        raise ForgeConnectionError(f"{where}: app is for GitHub")
    default = raw.get("default", False)
    if not isinstance(default, bool):
        raise ForgeConnectionError(f"{where}.default must be true or false")
    return ForgeUser(name=name, token_secret=secret, default=default, app=app,
                     transport=transport)


def _connection(host: str, raw: object) -> ForgeConnection:
    where = f"{FORGES_FILE}: {host}"
    if not _HOST_RE.match(host):
        raise ForgeConnectionError(f"{where}: a forge is keyed by its host")
    if not isinstance(raw, dict):
        raise ForgeConnectionError(f"{where} must be a mapping")
    unknown = set(raw) - _FORGE_KEYS
    if unknown:
        raise ForgeConnectionError(f"{where} has unknown key {sorted(unknown)[0]!r}")
    kind = _str(raw, "kind", where)
    if kind not in FORGE_KINDS:
        raise ForgeConnectionError(f"{where}.kind must be one of {', '.join(FORGE_KINDS)}")
    url = _str(raw, "url", where).rstrip("/")
    if host_of(url) != host:
        raise ForgeConnectionError(f"{where}.url must be on {host} (was {url!r})")
    users_raw = raw.get("users") or {}
    if not isinstance(users_raw, dict):
        raise ForgeConnectionError(f"{where}.users must be a mapping")
    users = tuple(_user(host, kind, str(n), u) for n, u in users_raw.items())
    if sum(u.default for u in users) > 1:
        raise ForgeConnectionError(f"{where}: only one user can be the default")
    return ForgeConnection(host=host, kind=kind, url=url,
                           ssh=(_str(raw, "ssh", where) or default_ssh(url)).rstrip("/"),
                           host_key=_str(raw, "host_key", where), users=users)


def load(path: Path | None = None) -> dict[str, ForgeConnection]:
    """Every forge this host connected, by host. {} when none is."""
    path = path or forges_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ForgeConnectionError(f"couldn't read {path}: {e}") from e
    try:
        fm, _ = parse_frontmatter(text)
    except YamlSubsetError as e:
        raise ForgeConnectionError(f"{path}: {e}") from e
    return {str(host): _connection(str(host), raw) for host, raw in fm.items()}


def connections() -> dict[str, ForgeConnection]:
    """The host's connection source (the file, for now)."""
    return load()


def get(host: str) -> ForgeConnection | None:
    return connections().get(host)


def secret_name(host: str, user: str, *, first: bool) -> str:
    """The host secrets file's name for a user's token: `<HOST>_TOKEN` for the
    first user on a forge, `<USER>_<HOST>_TOKEN` for another."""
    key = host if first else f"{user}@{host}"
    return re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_") + "_TOKEN"


def _dump(conn: ForgeConnection) -> dict[str, object]:
    d: dict[str, object] = {"kind": conn.kind, "url": conn.url, "ssh": conn.ssh}
    if conn.host_key:
        d["host_key"] = conn.host_key
    users: dict[str, object] = {}
    for u in conn.users:
        entry: dict[str, object] = {"token": secret_ref.stored(u.token_secret)}
        if u.default:
            entry["default"] = True
        if u.app:
            entry["app"] = u.app
        if u.transport:
            entry["transport"] = u.transport
        users[u.name] = entry
    d["users"] = users
    return d


_HEADER = ("The forges this host has connected, written by `bot-bottle forge connect`\n"
           "(ADR 0019). Tokens are in the host secrets file, named here.\n")


def save(found: dict[str, ForgeConnection], path: Path | None = None) -> None:
    path = path or forges_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict[str, object] = {host: _dump(conn) for host, conn in sorted(found.items())}
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(f"---\n{serialize_yaml_subset(fm) if fm else ''}---\n\n{_HEADER}",
                   encoding="utf-8")
    tmp.replace(path)


def add_user(conn: ForgeConnection, user: ForgeUser, *, make_default: bool = False,
             path: Path | None = None) -> ForgeConnection:
    """Record `conn` with `user` connected on it (replacing that user, and the
    forge's url, ssh and pin, if they were there). The first user is the
    default; another becomes it only with `make_default`."""
    found = load(path)
    before = found.get(conn.host)
    others = [u for u in (before.users if before else ()) if u.name != user.name]
    default = make_default or not others or any(
        u.default for u in (before.users if before else ()) if u.name == user.name)
    others = [replace(u, default=False) if default else u for u in others]
    updated = replace(conn, users=(*others, replace(user, default=default)))
    found[conn.host] = updated
    save(found, path)
    return updated


__all__ = ["FORGES_FILE", "TRANSPORTS", "ForgeConnectionError", "ForgeConnection", "ForgeUser",
           "add_user", "connections", "default_ssh", "forges_path", "get", "host_of", "load",
           "save", "secret_name"]
