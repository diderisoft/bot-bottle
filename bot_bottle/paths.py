"""Foundational filesystem paths for bot-bottle.

`bot_bottle_root()` is the app data root — state, queue, audit logs,
git-gate keys, and the shared DB all live under it. It defaults to
`~/.bot-bottle` and is overridable with the **`BOT_BOTTLE_ROOT`** env var.

The env override is the single knob for redirecting the root: the test
suite points it at a throwaway dir instead of monkey-patching the function
(every module and every flat/package copy reads the same env var, so one
override covers them all), and operators can relocate the root if needed.

This module has no bot-bottle imports, so it is safe to import from any
layer (and to COPY flat into the sidecar bundle).
"""

from __future__ import annotations

import os
from pathlib import Path

# The single shared host state DB. All bot-bottle SQLite stores (supervise
# queue, audit, the orchestrator registry) co-tenant this one file — the
# TableMigrations schema_key namespaces each store's tables.
HOST_DB_FILENAME = "bot-bottle.db"


def bot_bottle_root() -> Path:
    """The app data root — `$BOT_BOTTLE_ROOT` if set, else `~/.bot-bottle`."""
    override = os.environ.get("BOT_BOTTLE_ROOT")
    return Path(override) if override else Path.home() / ".bot-bottle"


def host_db_path() -> Path:
    """Path to the shared host state DB, `<root>/db/bot-bottle.db`.

    Kept in its own `db/` subdirectory (not directly under the root) so a
    backend that can only bind-mount *directories* can share this one file
    with a sidecar without exposing the root's other contents (git-gate
    keys, per-bottle state, ...)."""
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


__all__ = ["HOST_DB_FILENAME", "bot_bottle_root", "host_db_path"]
