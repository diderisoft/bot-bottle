"""An upstream SSH server as a bottle reaches it (ADR 0023): a forge of kind
`ssh`, declared in the bottle and granted by its path on the server, with
the test's key file connected on this host."""

from __future__ import annotations

from typing import Any
from unittest import mock

from bot_bottle.forge_connections import ForgeConnection, ForgeKey
from bot_bottle.forge_keys import check_file


def ssh_forge(url: str, key: str, host_key: str) -> tuple[dict[str, Any], "mock._patch[Any]"]:
    """The `git_gate` block granting the repo at `url`
    (`ssh://user@host[:port]/<path>`) with write, and a patch connecting its
    server on this host with `key` (a private key file, mode 600) for it.
    Start the patch before the manifest loads and stop it at teardown."""
    address, _, path = url.removeprefix("ssh://").partition("/")
    host = address.rsplit("@", 1)[-1].split(":", 1)[0]
    conn = ForgeConnection(
        host=host, kind="ssh", url="", ssh=f"ssh://{address}", host_key=host_key,
        keys=(ForgeKey(repo=path, path=key, fingerprint=check_file(key), write=True),))
    git_gate: dict[str, Any] = {
        "forges": {host: {"kind": "ssh", "ssh": conn.ssh, "host_key": host_key}},
        "repos": [f"{host}/{path}:write"],
    }
    patch = mock.patch("bot_bottle.forge_connections.connections",
                       new=lambda: {host: conn})
    return git_gate, patch


__all__ = ["ssh_forge"]
