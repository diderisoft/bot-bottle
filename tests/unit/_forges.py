"""Test helpers: forges this host has connected (`forge_connections`, ADR 0019)."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from unittest import mock

from bot_bottle.forge_connections import ForgeConnection, ForgeKey, ForgeStore, ForgeUser
from bot_bottle.manifest import ManifestIndex
from bot_bottle.manifest.git import parse_git_gate_config
from bot_bottle.paths import HOST_DB_FILENAME

GITEA = ForgeConnection(
    host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
    ssh="ssh://git@10.0.0.1:30009", host_key="ssh-ed25519 AAAA",
    users=(ForgeUser(name="bot", token_secret="GITEA_X", default=True),))

GITHUB = ForgeConnection(
    host="github.com", kind="github", url="https://github.com", ssh="ssh://git@github.com",
    host_key="ssh-ed25519 GH",
    users=(ForgeUser(name="octo", token_secret="GITHUB_COM_TOKEN", default=True,
                     app="bot-bottle"),))

# A plain SSH server (ADR 0023): a default key that can push, and a
# read-only deploy key for org/lib.
SSH = ForgeConnection(
    host="git.example.com", kind="ssh", url="", ssh="ssh://git@git.example.com:2222",
    host_key="ssh-ed25519 SSH",
    keys=(ForgeKey(repo="", path="/keys/default", fingerprint="SHA256:dflt", write=True),
          ForgeKey(repo="org/lib", path="/keys/lib", fingerprint="SHA256:lib")))


def connected(*conns: ForgeConnection) -> "mock._patch[object]":
    """Patch the host's connection source to `conns` (a context manager or
    decorator; a decorated test gets no extra argument)."""
    found = {c.host: c for c in conns}
    return mock.patch("bot_bottle.forge_connections.connections", new=lambda: dict(found))


def record(root: Path, *conns: ForgeConnection) -> ForgeStore:
    """Connect `conns` in the host DB under `root`, as `forge connect` would."""
    store = ForgeStore(root / "db" / HOST_DB_FILENAME)
    for conn in conns:
        if conn.kind == "ssh":
            store.connect_server(conn)
            for key in conn.keys:
                store.add_key(conn.host, key)
        for user in conn.users:
            store.add_user(conn, user, make_default=user.default)
    return store


def long_form_index(obj: dict[str, object]) -> ManifestIndex:
    """`ManifestIndex.from_json_obj(obj)`, except that each bottle's long-form
    repos, which a bottle file can't hold (ADR 0023), are set as if its grants
    had expanded to them (they don't pass through `extends`)."""
    obj = copy.deepcopy(obj)
    bottles = obj["bottles"]
    assert isinstance(bottles, dict)
    entries = {}
    for name, bottle in bottles.items():
        git = bottle.get("git_gate")
        if isinstance(git, dict) and isinstance(git.get("repos"), dict):
            entries[name] = parse_git_gate_config(name, {"repos": git.pop("repos")})[0]
    index = ManifestIndex.from_json_obj(obj)
    return replace(index, bottles={n: replace(b, git=entries.get(n, b.git))
                                   for n, b in index.bottles.items()})


__all__ = ["GITEA", "GITHUB", "SSH", "connected", "long_form_index", "record"]
