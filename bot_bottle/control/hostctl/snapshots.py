"""Snapshots of a running agent's work, taken from the console (PRD 0095 follow-up).

A snapshot pins a point in a session: the workspace's changes since launch as
a git patch (the same patch the host saves at teardown), with the
conversation's position then (the newest transcript `seq`). Each is kept as

  ~/.bot-bottle/snapshots/<session>/<activation>/<taken>.patch   (0600)

with a row of the host DB's `snapshots` pointing at it (when, the seq, files
and lines changed, the patch's size and SHA-256), and audited as
`snapshot.taken`. The row and the patch go when the bottled agent is removed
(`bottle_state.delete_bottled_agent`). Only a copied git workspace can be
snapshotted; restoring one is later work.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...lib.safe_path import check_id, under
from ...paths import bot_bottle_root, host_db_path

# Exporting the workspace's changes gives up after this long.
PATCH_TIMEOUT_SECONDS = 300.0

if TYPE_CHECKING:
    from ...backend import Bottle


class SnapshotUnavailable(Exception):
    """The bottle has nothing a snapshot can take."""


def snapshots_root() -> Path:
    return bot_bottle_root() / "snapshots"


class SnapshotStore(DbStore):
    """`snapshots`: each snapshot's record, by session, and the patch it
    points at (relative to `snapshots_root()`)."""

    TABLES = {"snapshots": MACHINE_SPECIFIC}
    _FIELDS = ("taken", "seq", "files", "added", "removed", "bytes", "sha256")

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("snapshots", [
            """
            CREATE TABLE snapshots (
                session TEXT NOT NULL,
                id TEXT NOT NULL,
                activation TEXT NOT NULL,
                taken REAL NOT NULL,
                seq INTEGER,
                files INTEGER NOT NULL,
                added INTEGER NOT NULL,
                removed INTEGER NOT NULL,
                bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                patch TEXT NOT NULL,
                PRIMARY KEY (session, id)
            )
            """,
        ]))
        self.migrate()

    def put(self, session: str, activation: str, record: dict[str, Any]) -> None:
        patch = f"{session}/{activation}/{record['id']}.patch"
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO snapshots VALUES "
                         "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (session, record["id"], activation,
                          *(record.get(f) for f in self._FIELDS), patch))
        self._chmod()

    def of(self, session: str) -> list[dict[str, Any]]:
        """`session`'s snapshots, oldest first, as `take` returned them."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM snapshots WHERE session = ? ORDER BY taken",
                                (session,)).fetchall()
        return [{"id": r["id"], **{f: r[f] for f in self._FIELDS}} for r in rows]


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def take(bottle: "Bottle", workspace: Any, *, session: str, activation: str,
         seq: int | None, clock: Any = time.time) -> dict[str, Any]:
    """Take a snapshot of `bottle`'s workspace; its record."""
    # pylint: disable=import-outside-toplevel
    from ...workspace import export_patch_script, patch_summary
    if not (workspace.enabled and workspace.has_host_git_dir):
        raise SnapshotUnavailable("only a workspace copied from a git checkout can be "
                                  "snapshotted (start the agent with --cwd in a git repo)")
    result = bottle.exec(export_patch_script(shlex.quote(workspace.guest_path)),
                         timeout=PATCH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SnapshotUnavailable(f"couldn't read the workspace: "
                                  f"{(result.stderr or result.stdout).strip()[:200]}")
    patch = result.stdout
    taken = clock()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(taken)) + f"-{int(taken * 1000) % 1000:03d}"
    run = under(snapshots_root(), check_id(session, "a session"),
                check_id(activation, "an activation"))
    for d in (snapshots_root(), run.parent, run):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = patch.encode("utf-8")
    files, added, removed = patch_summary(patch)
    record = {"id": stamp, "taken": taken, "seq": seq, "files": files, "added": added,
              "removed": removed, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    _write(run / f"{stamp}.patch", data)
    SnapshotStore(host_db_path()).put(session, activation, record)
    return record


__all__ = ["SnapshotStore", "SnapshotUnavailable", "snapshots_root", "take"]
