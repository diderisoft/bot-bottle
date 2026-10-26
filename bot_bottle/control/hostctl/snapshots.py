"""Snapshots of a running agent's work, taken from the console (PRD 0095 follow-up).

A snapshot pins a point in a session: the workspace's changes since launch as
a git patch (the same patch the host saves at teardown), with the
conversation's position then (the newest transcript `seq`). Each is kept as

  ~/.bot-bottle/snapshots/<session>/<activation>/<taken>.patch   (0600)

with `<taken>.json` beside it (when, the seq, files and lines changed, the
patch's size and SHA-256), and audited as `snapshot.taken`. Only a copied git
workspace can be snapshotted; restoring one is later work.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...paths import bot_bottle_root

if TYPE_CHECKING:
    from ...backend import Bottle


class SnapshotUnavailable(Exception):
    """The bottle has nothing a snapshot can take."""


def snapshots_root() -> Path:
    return bot_bottle_root() / "snapshots"


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
    result = bottle.exec(export_patch_script(shlex.quote(workspace.guest_path)))
    if result.returncode != 0:
        raise SnapshotUnavailable(f"couldn't read the workspace: "
                                  f"{(result.stderr or result.stdout).strip()[:200]}")
    patch = result.stdout
    taken = clock()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(taken)) + f"-{int(taken * 1000) % 1000:03d}"
    run = snapshots_root() / session / activation
    for d in (snapshots_root(), run.parent, run):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = patch.encode("utf-8")
    files, added, removed = patch_summary(patch)
    record = {"id": stamp, "taken": taken, "seq": seq, "files": files, "added": added,
              "removed": removed, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    _write(run / f"{stamp}.patch", data)
    _write(run / f"{stamp}.json", (json.dumps(record, indent=2) + "\n").encode())
    return record


__all__ = ["SnapshotUnavailable", "snapshots_root", "take"]
