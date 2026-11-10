"""Pairing with a console (ADR 0007): what `bot-bottle pair` keeps, and this
host's iroh identity. The link itself is `remote.py`.

In the host DB (ADR 0020), both machine-specific:

- `pairing`: the console's URL, this host's id on it, the console identity
  the link pins, whether the link runs, and the names of the host's console
  tokens in the host secrets file. The token values are only there.
- `console_link`: the link's state as it last was, for `bot-bottle status`.

The host controller is their only writer, `pair` and `login` included: the
CLI goes through `PAIRING_PATH`. `remote.key`, the iroh secret key, stays a
key file under `~/.bot-bottle/`.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...paths import bot_bottle_root

KEY_FILE = "remote.key"
# The console's tokens for this host, in the host secrets file.
ACCESS_TOKEN_SECRET = "CONSOLE_ACCESS_TOKEN"
REFRESH_TOKEN_SECRET = "CONSOLE_REFRESH_TOKEN"
# What a change may set: the pairing's own fields, and the tokens' values.
TOKENS = ("access_token", "refresh_token")


class RemoteError(Exception):
    """Remote access can't be configured or can't run."""


@dataclass(frozen=True)
class RemoteConfig:
    """What the link runs on: whether it runs, and the pinned console."""

    enabled: bool = False
    console_node_id: str = ""
    console_ticket: str = ""


@dataclass(frozen=True)
class Pairing:
    """This host's pairing with a console. The tokens are names in the host
    secrets file, "" when it has none."""

    url: str = ""
    host_id: str = ""
    console_node_id: str = ""
    console_ticket: str = ""
    enabled: bool = False
    access_token_secret: str = ""
    refresh_token_secret: str = ""

    @property
    def config(self) -> RemoteConfig:
        return RemoteConfig(enabled=self.enabled, console_node_id=self.console_node_id,
                            console_ticket=self.console_ticket)

    def require(self) -> "Pairing":
        """This pairing, if it's complete; else RemoteError saying so."""
        if not self.url or not self.access_token_secret:
            raise RemoteError("not paired with a console; run `bot-bottle pair` first")
        return self

    def tokens(self, secrets_path: Path | None = None) -> tuple[str, str]:
        """The access and refresh tokens' values ("" for one it hasn't), from
        the host secrets file."""
        # pylint: disable-next=import-outside-toplevel
        from ...secret_source import host_store
        secrets = host_store(secrets_path)
        values = [secrets.get(n) or "" if n else ""
                  for n in (self.access_token_secret, self.refresh_token_secret)]
        return values[0], values[1]

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> "Pairing":
        if not isinstance(raw, dict):
            raise ValueError("a pairing must be an object")
        return cls(**{f.name: raw[f.name] for f in fields(cls)
                      if isinstance(raw.get(f.name), type(getattr(cls(), f.name)))})


_SETTABLE = {f.name: type(getattr(Pairing(), f.name)) for f in fields(Pairing)
             if not f.name.endswith("_secret")}


class PairingStore(DbStore):
    """The host DB's pairing and link state. Only the host controller opens
    it."""

    # A pairing is this machine's: the console knows it by its iroh key.
    TABLES = {"pairing": MACHINE_SPECIFIC, "console_link": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("pairing", [
            # The tokens' columns name secrets in the host secrets file.
            """
            CREATE TABLE pairing (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                url TEXT NOT NULL DEFAULT '',
                host_id TEXT NOT NULL DEFAULT '',
                console_node_id TEXT NOT NULL DEFAULT '',
                console_ticket TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                access_token_secret TEXT NOT NULL DEFAULT '',
                refresh_token_secret TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE console_link (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                updated REAL NOT NULL,
                connected INTEGER NOT NULL DEFAULT 0,
                unpaired INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                console_node_id TEXT NOT NULL DEFAULT ''
            )
            """,
        ]))
        self.migrate()

    def pairing(self) -> Pairing:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM pairing WHERE id = 1").fetchone()
        if row is None:
            return Pairing()
        return Pairing(url=row["url"], host_id=row["host_id"],
                       console_node_id=row["console_node_id"],
                       console_ticket=row["console_ticket"], enabled=bool(row["enabled"]),
                       access_token_secret=row["access_token_secret"],
                       refresh_token_secret=row["refresh_token_secret"])

    def update(self, changes: dict[str, object], *, new: bool = False,
               secrets_path: Path | None = None) -> Pairing:
        """Set the pairing's fields and tokens `changes` names. A `new`
        pairing starts from nothing, so what it doesn't name is cleared.
        ValueError for an unknown field or a bad value."""
        # pylint: disable-next=import-outside-toplevel
        from ...secret_source import host_store, remove_secret, write_secret
        unknown = sorted(set(changes) - set(_SETTABLE) - set(TOKENS))
        if unknown:
            raise ValueError(f"unknown pairing field(s): {', '.join(unknown)}")
        for name, value in changes.items():
            want = str if name in TOKENS else _SETTABLE[name]
            if not isinstance(value, want) or (name in TOKENS and not value):
                raise ValueError(f"{name} must be a {'token' if name in TOKENS else want.__name__}")
        pairing = Pairing() if new else self.pairing()
        pairing = replace(pairing, **{k: v for k, v in changes.items() if k in _SETTABLE})
        path = host_store(secrets_path).path
        for name, secret, attr in (("access_token", ACCESS_TOKEN_SECRET, "access_token_secret"),
                                   ("refresh_token", REFRESH_TOKEN_SECRET,
                                    "refresh_token_secret")):
            if name in changes:
                write_secret(path, secret, str(changes[name]))
                pairing = replace(pairing, **{attr: secret})
            elif new:
                remove_secret(path, secret)
        row = asdict(pairing)
        with self._connection() as conn:
            conn.execute(f"INSERT OR REPLACE INTO pairing (id, {', '.join(row)}) "
                         f"VALUES (1, {', '.join('?' * len(row))})",
                         [int(v) if isinstance(v, bool) else v for v in row.values()])
        self._chmod()
        return pairing

    def status(self) -> dict[str, Any]:
        """The link's last state ({} before it has one): `updated`,
        `connected`, `unpaired`, `error` and `console_node_id`."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM console_link WHERE id = 1").fetchone()
        if row is None:
            return {}
        return {"updated": row["updated"], "connected": bool(row["connected"]),
                "unpaired": bool(row["unpaired"]), "error": row["error"],
                "console_node_id": row["console_node_id"]}

    def set_status(self, *, connected: bool = False, unpaired: bool = False, error: str = "",
                   console_node_id: str = "") -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO console_link VALUES (1, ?, ?, ?, ?, ?)",
                         (time.time(), int(connected), int(unpaired), error, console_node_id))
        self._chmod()


def _root() -> Path:
    return bot_bottle_root()


def store() -> PairingStore:
    # pylint: disable-next=import-outside-toplevel
    from ...paths import host_db_path
    return PairingStore(host_db_path())


def load_pairing() -> Pairing:
    return store().pairing()


def load_config() -> RemoteConfig:
    return load_pairing().config


def secret_key() -> bytes:
    """This host's iroh secret key (32 bytes, 0600), created on first use. Its
    public half is the node id."""
    path = _root() / KEY_FILE
    try:
        data = path.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def import_iroh() -> "Any":
    try:
        import iroh  # pylint: disable=import-outside-toplevel  # pyright: ignore[reportMissingImports]
    except ImportError as e:
        raise RemoteError(
            "remote access needs the iroh bindings: install `bot-bottle[remote]` "
            "(not available on Intel Macs)") from e
    return iroh


def node_id() -> str:
    """This host's iroh node id."""
    iroh = import_iroh()
    return str(iroh.SecretKey.from_bytes(secret_key()).public())


def write_status(*, connected: bool = False, unpaired: bool = False, error: str = "",
                 console_node_id: str = "") -> None:
    """The link's state now, for `bot-bottle status`."""
    try:
        store().set_status(connected=connected, unpaired=unpaired, error=error,
                           console_node_id=console_node_id)
    except (OSError, sqlite3.Error):
        pass


def read_status() -> dict[str, Any]:
    try:
        return store().status()
    except (OSError, sqlite3.Error):
        return {}


__all__ = [
    "ACCESS_TOKEN_SECRET", "KEY_FILE", "REFRESH_TOKEN_SECRET", "Pairing", "PairingStore",
    "RemoteConfig", "RemoteError", "import_iroh", "load_config", "load_pairing", "node_id",
    "read_status", "secret_key", "store", "write_status",
]
