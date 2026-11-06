"""Test helpers: forges this host has connected (`forge_connections`, ADR 0019)."""

from __future__ import annotations

from unittest import mock

from bot_bottle.forge_connections import ForgeConnection, ForgeUser

GITEA = ForgeConnection(
    host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
    ssh="ssh://git@10.0.0.1:30009", host_key="ssh-ed25519 AAAA",
    users=(ForgeUser(name="bot", token_secret="GITEA_X", default=True),))

GITHUB = ForgeConnection(
    host="github.com", kind="github", url="https://github.com", ssh="ssh://git@github.com",
    host_key="ssh-ed25519 GH",
    users=(ForgeUser(name="octo", token_secret="GITHUB_COM_TOKEN", default=True,
                     app="bot-bottle"),))


def connected(*conns: ForgeConnection) -> "mock._patch[object]":
    """Patch the host's connection source to `conns` (a context manager or
    decorator; a decorated test gets no extra argument)."""
    found = {c.host: c for c in conns}
    return mock.patch("bot_bottle.forge_connections.connections", new=lambda: dict(found))


__all__ = ["GITEA", "GITHUB", "connected"]
