"""Foundational filesystem paths for bot-bottle.

`bot_bottle_root()` is the app data root — state, queue, audit logs,
git-gate keys, and the shared DB all live under it. It defaults to
`~/.bot-bottle` and is overridable with the **`BOT_BOTTLE_ROOT`** env var.

The env override is the single knob for redirecting the root: the test
suite points it at a throwaway dir instead of monkey-patching the function
(every module and every flat/package copy reads the same env var, so one
override covers them all), and operators can relocate the root if needed.

This module has no bot-bottle imports, so it is safe to import from any
layer (and to COPY flat into the gateway).
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

# The single shared host state DB. All bot-bottle SQLite stores (supervise
# queue, audit, the orchestrator registry) co-tenant this one file — the
# TableMigrations schema_key namespaces each store's tables.
HOST_DB_FILENAME = "bot-bottle.db"

# The per-host control-plane secret file, and the env var the launchers inject
# its value into. The control plane requires this secret on every mutating /
# reading route (see orchestrator/control_plane.py); it is held only by the
# trusted callers (control plane, gateway, host CLI) and never handed to an
# agent, so an agent that can reach the control-plane port still can't drive it.
CONTROL_PLANE_TOKEN_FILENAME = "control-plane-token"
CONTROL_PLANE_TOKEN_ENV = "BOT_BOTTLE_CONTROL_PLANE_TOKEN"


def bot_bottle_root() -> Path:
    """The app data root — `$BOT_BOTTLE_ROOT` if set, else `~/.bot-bottle`."""
    override = os.environ.get("BOT_BOTTLE_ROOT")
    return Path(override) if override else Path.home() / ".bot-bottle"


def host_db_path() -> Path:
    """Path to the shared host state DB, `<root>/db/bot-bottle.db`.

    Kept in its own `db/` subdirectory (not directly under the root) so a
    backend that can only bind-mount *directories* can share this one file
    with a gateway without exposing the root's other contents (git-gate
    keys, per-bottle state, ...)."""
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


def host_db_dir() -> Path:
    """The directory holding the shared host state DB, created if missing.
    Backends bind-mount this into their gateway so the supervise daemon writes
    to the one DB the orchestrator (and the operator over HTTP) reads."""
    db_dir = host_db_path().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def host_control_plane_token() -> str:
    """The per-host control-plane secret, minted (256-bit, url-safe) and
    persisted 0600 on first use, then reused.

    This is the shared secret the launchers inject into the control-plane and
    gateway containers and that the host CLI presents on every call. It is a
    *host* artifact — the file lives under the root the agent never mounts, and
    the env var is set only on the trusted containers — so reading it here is
    safe on the host launch path but the value never reaches a bottle."""
    path = bot_bottle_root() / CONTROL_PLANE_TOKEN_FILENAME
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Create 0600 up front (O_EXCL loses a concurrent race harmlessly — we
    # re-read the winner's token below) so the secret is never briefly world-
    # readable between write and chmod.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return token


__all__ = [
    "HOST_DB_FILENAME",
    "CONTROL_PLANE_TOKEN_FILENAME",
    "CONTROL_PLANE_TOKEN_ENV",
    "bot_bottle_root",
    "host_db_path",
    "host_db_dir",
    "host_control_plane_token",
]
