"""The forges this host has connected (ADR 0019), in the host DB (ADR 0020):
`forge_connections` and `forge_users`.

A bottle describes a forge; the host connects it. These tables hold what this
machine connected, keyed by the forge's host, the same key a repo grant's
prefix names: its kind, URL, the SSH address this machine reaches it on and
the host key pinned when it was connected, and each user connected on it.

Only `bot-bottle forge connect` (and the console's connect, and `forge
remove`) writes them, a user's token as `secret:NAME`, a name in the host
secrets file. A user's name is what the forge says the token belongs to. A
launch uses these, never a bottle's own description of the forge, so a token
goes only where its host connected it.

`connections()` is the host's connection source; a launch asks it, not the
tables. In the host controller it reads them (`ForgeStore`); anywhere else it
asks the host controller (`FORGES_PATH`), which is their only writer.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import host_access, secret_ref
from .contract.forges import FORGE_KINDS
from .lib.db_store import PORTABLE, DbStore
from .lib.migrations import TableMigrations
from .manifest.util import ManifestError

TRANSPORTS = ("https", "deploy-key")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ForgeConnectionError(ManifestError):
    """A connection that isn't well formed, the host's connections can't be
    read, or a grant names what isn't connected. A manifest error: a bottle
    that needs it doesn't load."""


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

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "token": secret_ref.stored(self.token_secret),
                "default": self.default, "app": self.app, "transport": self.transport}


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

    def to_json(self) -> dict[str, object]:
        return {"host": self.host, "kind": self.kind, "url": self.url, "ssh": self.ssh,
                "host_key": self.host_key, "users": [u.to_json() for u in self.users]}

    @classmethod
    def from_json(cls, raw: object) -> "ForgeConnection":
        """A connection in `to_json`'s shape, checked as connecting checks it.
        ForgeConnectionError saying what's wrong."""
        body = _object(raw, "forge")
        host = _str(body, "host", "forge")
        where = f"forge {host}"
        if not _HOST_RE.match(host):
            raise ForgeConnectionError(f"{where}: a forge is keyed by its host")
        kind = _str(body, "kind", where)
        if kind not in FORGE_KINDS:
            raise ForgeConnectionError(f"{where}: kind must be one of {', '.join(FORGE_KINDS)}")
        url = _str(body, "url", where).rstrip("/")
        if host_of(url) != host:
            raise ForgeConnectionError(f"{where}: url must be on {host} (was {url!r})")
        users_raw = body.get("users") or []
        if not isinstance(users_raw, list):
            raise ForgeConnectionError(f"{where}: users must be a list")
        users = tuple(_user(host, kind, u) for u in users_raw)
        if len({u.name for u in users}) != len(users):
            raise ForgeConnectionError(f"{where}: a user is connected once")
        if sum(u.default for u in users) > 1:
            raise ForgeConnectionError(f"{where}: only one user can be the default")
        return cls(host=host, kind=kind, url=url,
                   ssh=(_str(body, "ssh", where) or default_ssh(url)).rstrip("/"),
                   host_key=_str(body, "host_key", where), users=users)


def default_ssh(url: str) -> str:
    return f"ssh://git@{urlparse(url).hostname or ''}"


def host_of(url: str) -> str:
    """A forge's key from its URL: the host, with a port if it has one."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return f"{host}:{parsed.port}" if parsed.port else host


def _object(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ForgeConnectionError(f"{where} must be an object")
    return {str(k): v for k, v in raw.items()}  # type: ignore[misc]


def _str(d: dict[str, object], key: str, where: str) -> str:
    value = d.get(key, "")
    if not isinstance(value, str):
        raise ForgeConnectionError(f"{where}: {key} must be a string")
    return value


def _user(host: str, kind: str, raw: object) -> ForgeUser:
    body = _object(raw, f"forge {host}: a user")
    name = _str(body, "name", f"forge {host}: a user")
    where = f"forge {host}: user {name}"
    if not _USER_RE.match(name):
        raise ForgeConnectionError(f"{where}: a user is the forge's username")
    secret = secret_ref.stored_name(_str(body, "token", where))
    if not secret_ref.is_name(secret):
        raise ForgeConnectionError(f"{where}: token names a host secret, as secret:NAME")
    transport = _str(body, "transport", where)
    allowed = TRANSPORTS if kind == "github" else ("", "deploy-key")
    if transport and transport not in allowed:
        named = " or ".join(a for a in allowed if a)
        raise ForgeConnectionError(f"{where}: transport must be {named}")
    app = _str(body, "app", where)
    if app and kind != "github":
        raise ForgeConnectionError(f"{where}: app is for GitHub")
    default = body.get("default", False)
    if not isinstance(default, bool):
        raise ForgeConnectionError(f"{where}: default must be true or false")
    return ForgeUser(name=name, token_secret=secret, default=default, app=app,
                     transport=transport)


class ForgeStore(DbStore):
    """The host DB's forge connections. Only the host controller opens it."""

    # A connection travels with the host's owner, except its `ssh`: an
    # override is the address this machine reaches the forge on (over a VPN,
    # say), so it's machine-specific (ADR 0020).
    TABLES = {"forge_connections": PORTABLE, "forge_users": PORTABLE}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("forge_connections", [
            """
            CREATE TABLE forge_connections (
                host TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                ssh TEXT NOT NULL,
                host_key TEXT NOT NULL DEFAULT ''
            )
            """,
            # `token`: the secret's name, `secret:NAME`, never its value.
            """
            CREATE TABLE forge_users (
                host TEXT NOT NULL,
                user TEXT NOT NULL,
                token TEXT NOT NULL,
                transport TEXT NOT NULL DEFAULT '',
                app TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (host, user)
            )
            """,
        ]))
        self.migrate()

    def connections(self) -> dict[str, ForgeConnection]:
        """Every forge connected, by host; users in the order connected."""
        with self._connection() as conn:
            return self._read(conn)

    @staticmethod
    def _read(conn: sqlite3.Connection) -> dict[str, ForgeConnection]:
        users: dict[str, list[dict[str, object]]] = {}
        for row in conn.execute("SELECT * FROM forge_users ORDER BY rowid"):
            users.setdefault(row["host"], []).append(
                {"name": row["user"], "token": row["token"], "transport": row["transport"],
                 "app": row["app"], "default": bool(row["is_default"])})
        return {row["host"]: ForgeConnection.from_json(
            {**dict(row), "users": users.get(row["host"], [])})
            for row in conn.execute("SELECT * FROM forge_connections ORDER BY host")}

    def add_user(self, conn: ForgeConnection, user: ForgeUser, *,
                 make_default: bool = False) -> ForgeConnection:
        """Record `conn` with `user` connected on it (replacing that user, and
        the forge's url, ssh and pin, if they were there). The first user is
        the default; another becomes it only with `make_default`."""
        ForgeConnection.from_json({**conn.to_json(), "users": [user.to_json()]})
        # Whether it's the default depends on who else is connected, so two
        # first users at once can't both become it.
        with self._write_connection() as db:
            before = self._read(db).get(conn.host)
            was = [u for u in (before.users if before else ()) if u.name == user.name]
            others = [u for u in (before.users if before else ()) if u.name != user.name]
            default = make_default or not others or any(u.default for u in was)
            db.execute("INSERT OR REPLACE INTO forge_connections VALUES (?, ?, ?, ?, ?)",
                       (conn.host, conn.kind, conn.url, conn.ssh, conn.host_key))
            if default:
                db.execute("UPDATE forge_users SET is_default = 0 WHERE host = ?", (conn.host,))
            db.execute("DELETE FROM forge_users WHERE host = ? AND user = ?",
                       (conn.host, user.name))
            db.execute("INSERT INTO forge_users VALUES (?, ?, ?, ?, ?, ?)",
                       (conn.host, user.name, secret_ref.stored(user.token_secret),
                        user.transport, user.app, int(default)))
            updated = self._read(db)[conn.host]
        self._chmod()
        return updated

    def remove(self, host: str, user: str = "") -> list[ForgeUser]:
        """Disconnect `user` from `host`, or the whole forge when no user is
        named; a forge goes with its last user. The users removed."""
        with self._write_connection() as db:
            before = self._read(db).get(host)
            if before is None:
                raise ForgeConnectionError(f"{host} isn't connected on this host")
            removed = [before.user(user)] if user else list(before.users)
            for u in removed:
                db.execute("DELETE FROM forge_users WHERE host = ? AND user = ?", (host, u.name))
            if len(removed) == len(before.users):
                db.execute("DELETE FROM forge_connections WHERE host = ?", (host,))
        self._chmod()
        return removed


def _store() -> ForgeStore:
    # pylint: disable-next=import-outside-toplevel
    from .paths import host_db_path
    return ForgeStore(host_db_path())


def _ask(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    """The host controller's answer on `path`, as ForgeConnectionError when it
    refuses or can't be reached."""
    # pylint: disable-next=import-outside-toplevel
    from .control.hostctl.runner import HostControlError
    try:
        client = host_access.client()
        return client.get(path) if body is None else client.post(path, body)
    except HostControlError as e:
        raise ForgeConnectionError(getattr(e, "error", "") or str(e)) from e


def connections() -> dict[str, ForgeConnection]:
    """The host's connection source: every forge connected, by host."""
    if host_access.in_host_controller():
        return _store().connections()
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGES_PATH
    found = _ask(FORGES_PATH).get("forges")
    return {c.host: c for c in (ForgeConnection.from_json(f)
                                for f in (found if isinstance(found, list) else []))}


def get(host: str) -> ForgeConnection | None:
    return connections().get(host)


def add_user(conn: ForgeConnection, user: ForgeUser, *,
             make_default: bool = False) -> ForgeConnection:
    """`ForgeStore.add_user`, wherever this process is."""
    if host_access.in_host_controller():
        return _store().add_user(conn, user, make_default=make_default)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGES_PATH
    reply = _ask(FORGES_PATH, {"forge": {**conn.to_json(), "users": []},
                               "user": user.to_json(), "make_default": make_default})
    return ForgeConnection.from_json(reply.get("forge"))


def forget(host: str, user: str = "") -> list[str]:
    """The host controller's `remove`: drop the rows, then the removed users'
    tokens from the host secrets file. The users' names."""
    # pylint: disable=import-outside-toplevel
    from .contrib.github.user_token import CLIENT_ID_SUFFIX, EXPIRES_SUFFIX, REFRESH_SUFFIX
    from .paths import host_secrets_path
    from .secret_source import remove_secret
    removed = _store().remove(host, user)
    for u in removed:
        for suffix in ("", REFRESH_SUFFIX, EXPIRES_SUFFIX, CLIENT_ID_SUFFIX):
            remove_secret(host_secrets_path(), u.token_secret + suffix)
    return [u.name for u in removed]


def remove(host: str, user: str = "") -> list[str]:
    """Disconnect `user` (or every user) from `host` and drop their tokens.
    The users removed."""
    if host_access.in_host_controller():
        return forget(host, user)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGES_REMOVE_PATH
    removed = _ask(FORGES_REMOVE_PATH, {"host": host, "user": user}).get("removed")
    return [str(u) for u in (removed if isinstance(removed, list) else [])]


def secret_name(host: str, user: str, *, first: bool) -> str:
    """The host secrets file's name for a user's token: `<HOST>_TOKEN` for the
    first user on a forge, `<USER>_<HOST>_TOKEN` for another."""
    key = host if first else f"{user}@{host}"
    return re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_") + "_TOKEN"


__all__ = ["TRANSPORTS", "ForgeConnection", "ForgeConnectionError", "ForgeStore", "ForgeUser",
           "add_user", "connections", "default_ssh", "forget", "get", "host_of", "remove",
           "secret_name"]
