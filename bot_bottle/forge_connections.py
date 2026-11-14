"""The forges this host has connected (ADR 0019), in the host DB (ADR 0020):
`forge_connections` and `forge_users`.

A bottle describes a forge; the host connects it. These tables hold what this
machine connected, keyed by the forge's host, the same key a repo grant's
prefix names: its kind, URL, the SSH address this machine reaches it on and
the host key pinned when it was connected, and each user connected on it.
An `ssh` forge (a plain SSH git server, ADR 0023) has no users and no URL:
its connection holds keys instead (`forge_keys`), each a key file on this
host, by path and fingerprint, for one repo or as the forge's default.

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
from .lib.db_store import MACHINE_SPECIFIC, MIXED, PORTABLE, DbStore
from .lib.migrations import TableMigrations
from .manifest.util import ManifestError

TRANSPORTS = ("https", "deploy-key")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]+)?$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# A key's repo: `<owner>/<repo>`, or "" for the forge's default key.
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SSH_RE = re.compile(r"^ssh://[^@/\s]+@[A-Za-z0-9.-]+(?::[0-9]+)?$")


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
class ForgeKey:
    """A key file an `ssh` forge's connection holds, by reference (ADR 0023):
    its `path` on this host, never a copy, and its `fingerprint`
    (`ssh-keygen -lf`) as last seen. `repo` is `<owner>/<repo>`, or "" for
    the forge's default key. `write` is stated when it's added: there's no
    API to ask the server."""

    repo: str
    path: str
    fingerprint: str
    write: bool = False

    def to_json(self) -> dict[str, object]:
        return {"repo": self.repo, "path": self.path, "fingerprint": self.fingerprint,
                "write": self.write}

    @classmethod
    def from_json(cls, host: str, raw: object) -> "ForgeKey":
        body = _object(raw, f"forge {host}: a key")
        repo = _str(body, "repo", f"forge {host}: a key")
        where = f"forge {host}: the key for {repo or 'every repo'}"
        if repo and not REPO_RE.match(repo):
            raise ForgeConnectionError(f"{where}: a key's repo is <owner>/<repo>")
        path = _str(body, "path", where)
        if not path.startswith("/"):
            raise ForgeConnectionError(f"{where}: path must be absolute")
        fingerprint = _str(body, "fingerprint", where)
        if not fingerprint:
            raise ForgeConnectionError(f"{where}: no fingerprint")
        write = body.get("write", False)
        if not isinstance(write, bool):
            raise ForgeConnectionError(f"{where}: write must be true or false")
        return cls(repo=repo, path=path, fingerprint=fingerprint, write=write)


@dataclass(frozen=True)
class ForgeConnection:
    """One forge as this machine connected it."""

    host: str
    kind: str
    url: str
    ssh: str
    host_key: str = ""
    users: tuple[ForgeUser, ...] = field(default_factory=tuple)
    # An `ssh` forge's keys; other kinds have none.
    keys: tuple[ForgeKey, ...] = field(default_factory=tuple)

    def key_for(self, repo: str) -> ForgeKey | None:
        """The key a grant on `repo` (`<owner>/<repo>`) uses: its own, else the
        forge's default, else None."""
        found = {k.repo: k for k in self.keys}
        return found.get(repo) or found.get("")

    def user(self, name: str = "") -> ForgeUser:
        """`name`, else the default user, else the only one."""
        if self.kind == "ssh":
            raise ForgeConnectionError(
                f"{self.host} is an SSH server: it has keys, not users (`bot-bottle forge "
                f"key list {self.host}`)")
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
                "host_key": self.host_key, "users": [u.to_json() for u in self.users],
                "keys": [k.to_json() for k in self.keys]}

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
        if kind == "ssh":
            return _ssh_connection(host, body)
        if body.get("keys"):
            raise ForgeConnectionError(f"{where}: only an SSH server's connection holds keys")
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


def _ssh_connection(host: str, body: dict[str, object]) -> ForgeConnection:
    """An `ssh` forge's connection: its SSH address, on the host it's keyed
    by, and its keys; no URL and no users."""
    where = f"forge {host}"
    if _str(body, "url", where) or body.get("users"):
        raise ForgeConnectionError(f"{where}: an SSH server has no URL and no users")
    ssh = _str(body, "ssh", where).rstrip("/")
    if not _SSH_RE.match(ssh) or ssh_host(ssh) != host:
        raise ForgeConnectionError(f"{where}: ssh must be ssh://user@{host}[:port] "
                                   f"(was {ssh!r})")
    keys_raw = body.get("keys") or []
    if not isinstance(keys_raw, list):
        raise ForgeConnectionError(f"{where}: keys must be a list")
    keys = tuple(ForgeKey.from_json(host, k) for k in keys_raw)
    if len({k.repo for k in keys}) != len(keys):
        raise ForgeConnectionError(f"{where}: a repo has one key")
    return ForgeConnection(host=host, kind="ssh", url="", ssh=ssh,
                           host_key=_str(body, "host_key", where), keys=keys)


def ssh_host(ssh: str) -> str:
    """The host of `ssh://user@host[:port]`, without the port: what an `ssh`
    forge is keyed by, so a grant names the server, not its port."""
    return ssh.removeprefix("ssh://").split("@", 1)[-1].split(":", 1)[0].lower()


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
    # An `ssh` forge's keys are files on this machine (ADR 0023).
    TABLES = {"forge_connections": MIXED, "forge_users": PORTABLE,
              "forge_keys": MACHINE_SPECIFIC}
    COLUMNS = {"forge_connections": {"host": PORTABLE, "kind": PORTABLE, "url": PORTABLE,
                                     "ssh": MACHINE_SPECIFIC, "host_key": PORTABLE}}

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
            # An `ssh` forge's keys: `repo` is <owner>/<repo>, or '' for the
            # forge's default. A path, never the key.
            """
            CREATE TABLE forge_keys (
                host TEXT NOT NULL,
                repo TEXT NOT NULL,
                path TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                can_write INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (host, repo)
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
        keys: dict[str, list[dict[str, object]]] = {}
        for row in conn.execute("SELECT * FROM forge_keys ORDER BY repo"):
            keys.setdefault(row["host"], []).append(
                {"repo": row["repo"], "path": row["path"], "fingerprint": row["fingerprint"],
                 "write": bool(row["can_write"])})
        return {row["host"]: ForgeConnection.from_json(
            {**dict(row), "users": users.get(row["host"], []), "keys": keys.get(row["host"], [])})
            for row in conn.execute("SELECT * FROM forge_connections ORDER BY host")}

    def connect_server(self, conn: ForgeConnection) -> ForgeConnection:
        """Record the `ssh` forge `conn` (its address and pin, replacing them
        if it was there), keeping the keys it holds."""
        if conn.kind != "ssh":
            raise ForgeConnectionError(f"{conn.host}: only an SSH server connects without a user")
        ForgeConnection.from_json({**conn.to_json(), "keys": []})
        with self._write_connection() as db:
            _same_kind(self._read(db).get(conn.host), conn)
            db.execute("INSERT OR REPLACE INTO forge_connections VALUES (?, ?, ?, ?, ?)",
                       (conn.host, conn.kind, "", conn.ssh, conn.host_key))
            updated = self._read(db)[conn.host]
        self._chmod()
        return updated

    def add_key(self, host: str, key: ForgeKey) -> ForgeConnection:
        """Record `key` on the `ssh` forge `host`, replacing the one its repo
        (or the default) had."""
        ForgeKey.from_json(host, key.to_json())
        with self._write_connection() as db:
            before = self._read(db).get(host)
            if before is None or before.kind != "ssh":
                raise ForgeConnectionError(
                    f"{host} isn't an SSH server connected on this host; connect it with "
                    f"`bot-bottle forge connect ssh://git@{host}`" if before is None else
                    f"{host} is a {before.kind} forge: its keys are minted per launch")
            db.execute("INSERT OR REPLACE INTO forge_keys VALUES (?, ?, ?, ?, ?)",
                       (host, key.repo, key.path, key.fingerprint, int(key.write)))
            updated = self._read(db)[host]
        self._chmod()
        return updated

    def remove_key(self, host: str, repo: str) -> ForgeKey:
        """Drop `host`'s key for `repo` ("" for the default); the file stays."""
        with self._write_connection() as db:
            before = self._read(db).get(host)
            key = next((k for k in before.keys if k.repo == repo), None) if before else None
            if key is None:
                raise ForgeConnectionError(f"{host} holds no key for {repo or 'every repo'}")
            db.execute("DELETE FROM forge_keys WHERE host = ? AND repo = ?", (host, repo))
        self._chmod()
        return key

    def refingerprint(self, path: str, fingerprint: str) -> int:
        """Every key at `path` now has `fingerprint` (the file was replaced in
        place); how many references changed."""
        if not path or not fingerprint:
            raise ForgeConnectionError("a key's path and fingerprint")
        with self._write_connection() as db:
            changed = db.execute(
                "UPDATE forge_keys SET fingerprint = ? WHERE path = ? AND fingerprint != ?",
                (fingerprint, path, fingerprint)).rowcount
        self._chmod()
        return changed

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
            _same_kind(before, conn)
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
        named; a forge goes with its last user, and when the default goes the
        earliest connected user left becomes it. The users removed."""
        with self._write_connection() as db:
            before = self._read(db).get(host)
            if before is None:
                raise ForgeConnectionError(f"{host} isn't connected on this host")
            removed = [before.user(user)] if user else list(before.users)
            for u in removed:
                db.execute("DELETE FROM forge_users WHERE host = ? AND user = ?", (host, u.name))
            if len(removed) == len(before.users):
                db.execute("DELETE FROM forge_connections WHERE host = ?", (host,))
                db.execute("DELETE FROM forge_keys WHERE host = ?", (host,))
            elif any(u.default for u in removed):
                left = [u for u in before.users if u not in removed]
                db.execute("UPDATE forge_users SET is_default = 1 WHERE host = ? AND user = ?",
                           (host, left[0].name))
        self._chmod()
        return removed


def _same_kind(before: ForgeConnection | None, conn: ForgeConnection) -> None:
    """Connecting again keeps a forge's kind: an SSH server's keys, or a
    forge's users, would be left on the wrong kind."""
    if before is not None and before.kind != conn.kind:
        raise ForgeConnectionError(
            f"{conn.host} is connected as {before.kind}, not {conn.kind}; remove it first "
            f"(`bot-bottle forge remove {conn.host}`)")


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


def connect_server(conn: ForgeConnection) -> ForgeConnection:
    """`ForgeStore.connect_server`, wherever this process is."""
    if host_access.in_host_controller():
        return _store().connect_server(conn)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGES_PATH
    reply = _ask(FORGES_PATH, {"forge": {**conn.to_json(), "keys": []}})
    return ForgeConnection.from_json(reply.get("forge"))


def add_key(host: str, key: ForgeKey) -> ForgeConnection:
    """`ForgeStore.add_key`, wherever this process is."""
    if host_access.in_host_controller():
        return _store().add_key(host, key)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGE_KEYS_PATH
    return ForgeConnection.from_json(
        _ask(FORGE_KEYS_PATH, {"host": host, "key": key.to_json()}).get("forge"))


def remove_key(host: str, repo: str) -> ForgeKey:
    """`ForgeStore.remove_key`, wherever this process is."""
    if host_access.in_host_controller():
        return _store().remove_key(host, repo)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGE_KEYS_REMOVE_PATH
    return ForgeKey.from_json(host, _ask(FORGE_KEYS_REMOVE_PATH,
                                         {"host": host, "repo": repo}).get("key"))


def refingerprint(path: str, fingerprint: str) -> int:
    """`ForgeStore.refingerprint`, wherever this process is."""
    if host_access.in_host_controller():
        return _store().refingerprint(path, fingerprint)
    # pylint: disable-next=import-outside-toplevel
    from .contract.hostctl import FORGE_KEYS_PATH
    changed = _ask(FORGE_KEYS_PATH, {"path": path, "fingerprint": fingerprint}).get("changed")
    return changed if isinstance(changed, int) else 0


def forget(host: str, user: str = "") -> list[str]:
    """The host controller's `remove`: drop the rows, then the removed users'
    tokens from the host secrets file. The users' names."""
    # pylint: disable=import-outside-toplevel
    from . import forge_alerts
    from .contrib.github.user_token import CLIENT_ID_SUFFIX, EXPIRES_SUFFIX, REFRESH_SUFFIX
    from .paths import host_secrets_path
    from .secret_source import remove_secret
    removed = _store().remove(host, user)
    for u in removed:
        for suffix in ("", REFRESH_SUFFIX, EXPIRES_SUFFIX, CLIENT_ID_SUFFIX):
            remove_secret(host_secrets_path(), u.token_secret + suffix)
        forge_alerts.clear(u.token_secret)  # an alert for a user who's gone
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


def secret_name(host: str, user: str) -> str:
    """The host secrets file's name for `user`'s token on `host`:
    `BB_HOST_FORGE_<HOST>__<USER>_TOKEN`, under the reserved prefix no manifest
    can read. Each part is upper-cased with every run of other characters as
    one `_`, so `__` only ever separates the two. Two forges or users that
    differ only in punctuation share a name; `forge_connect` refuses the
    second."""
    def part(s: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "_", s.upper()).strip("_")
    return f"{secret_ref.HOST_PREFIX}FORGE_{part(host)}__{part(user)}_TOKEN"


__all__ = ["REPO_RE", "TRANSPORTS", "ForgeConnection", "ForgeConnectionError", "ForgeKey",
           "ForgeStore", "ForgeUser", "add_key", "add_user", "connect_server", "connections",
           "default_ssh", "forget", "get", "host_of", "refingerprint", "remove", "remove_key",
           "secret_name", "ssh_host"]
