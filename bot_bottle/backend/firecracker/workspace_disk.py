"""A project's persistent workspace disk on Firecracker (opt-in, #641).

Firecracker has no virtio-fs or 9p, so the project is copied into the bottle
and its changes come back as a patch, as on every backend. With
`BOT_BOTTLE_FC_WORKSPACE_DISK` set, the copy lands on an ext4 disk kept for
the project instead of in the bottle's home, so what the agent installed or
built there (anything the project ignores) is still there next run.

One disk per project, agent and bottle stack, per user: a different bottle
stack gets its own, so nothing one bottle's agent left behind runs in a bottle
with other permissions. The disk is only ever attached to a guest, never
mounted on the host, and one guest at a time.

What a later run sees: the project's files as they are on the host now (the
filtered copy again; the last run's changes came back as its patch), and the
ignored files the last run left, such as `node_modules` or a build dir.

Which project, agent and bottles each disk is for, and which bottled agent has
it, is a row of the host DB's `workspace_disks`, keyed by the disk's name. The
host controller, where launches run, writes it; `cleanup workspaces` reads and
removes through it (`WORKSPACE_DISKS_PATH`).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, cast

from ... import host_access
from ...contract.hostctl import WORKSPACE_DISKS_PATH, WORKSPACE_DISKS_REMOVE_PATH
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.log import die, info, warn
from ...lib.migrations import TableMigrations
from ...workspace import DiskInfo

DEFAULT_SIZE = "16G"
DIR_NAME = "workspace-disks"
# The bottle state file that says its last run used a workspace disk: its
# home's workspace dir is then an empty mount point, not the agent's copy.
STATE_MARKER = "workspace-disk"

_SIZE_RE = re.compile(r"^([1-9][0-9]*)([MG])$")
_MIN_MIB = 512
_MAX_MIB = 1024 * 1024
_GUEST_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
# A disk's name (`disk_key`).
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


def requested_size(setting: str) -> str | None:
    """The disk size `BOT_BOTTLE_FC_WORKSPACE_DISK` asks for, or None when
    it's off. `1` or `on` is the default size; else `<n>M` or `<n>G`."""
    value = setting.strip()
    if value.lower() in ("", "0", "off", "no", "false"):
        return None
    if value.lower() in ("1", "on", "yes", "true"):
        return DEFAULT_SIZE
    match = _SIZE_RE.match(value.upper())
    if not match:
        die(f"BOT_BOTTLE_FC_WORKSPACE_DISK={setting!r}: expected 1, or a size like 16G or 8192M")
    mib = int(match[1]) * (1024 if match[2] == "G" else 1)
    if not _MIN_MIB <= mib <= _MAX_MIB:
        die(f"BOT_BOTTLE_FC_WORKSPACE_DISK={setting!r}: between {_MIN_MIB}M and "
            f"{_MAX_MIB // 1024}G")
    return f"{mib}M"


def disk_dir() -> Path:
    """`<state root>/workspace-disks/`: kept, and counted as workspaces."""
    from ...paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
    return bot_bottle_root() / DIR_NAME


def disk_key(project: Path, agent: str, bottles: tuple[str, ...]) -> str:
    """The disk's name: the project's real path, the agent and the bottle
    stack (in launch order, since order decides the merged bottle)."""
    ident = json.dumps([str(project.resolve()), agent, list(bottles)])
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


@dataclass
class WorkspaceDisk:
    """An attached disk: held (flock) until `release`."""

    path: Path
    fresh: bool
    # The bottled agent that used it last, before this one.
    last_slug: str
    _lock: IO[str] | None = field(default=None, repr=False)

    def release(self) -> None:
        """Mark it free and drop the lock. Idempotent."""
        if self._lock is None:
            return
        meta = _read_meta(self.path)
        if meta.get("owner"):
            meta["owner"] = ""
            _write_meta(self.path, meta)
        self._lock.close()
        self._lock = None


class WorkspaceDiskStore(DbStore):
    """`workspace_disks`: each disk's project, agent and bottles, the bottled
    agent that has it (`owner`, "" for none) and the one that used it last."""

    TABLES = {"workspace_disks": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("workspace_disks", [
            """
            CREATE TABLE workspace_disks (
                key TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                agent TEXT NOT NULL,
                bottles TEXT NOT NULL,
                owner TEXT NOT NULL,
                last_slug TEXT NOT NULL
            )
            """,
        ]))
        self.migrate()

    @staticmethod
    def _meta(row: Any) -> dict[str, object]:
        return {"project": row["project"], "agent": row["agent"],
                "bottles": json.loads(row["bottles"]), "owner": row["owner"],
                "last_slug": row["last_slug"]}

    def all(self) -> dict[str, dict[str, object]]:
        with self._connection() as conn:
            return {r["key"]: self._meta(r)
                    for r in conn.execute("SELECT * FROM workspace_disks")}

    def get(self, key: str) -> dict[str, object]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM workspace_disks WHERE key = ?", (key,)).fetchone()
        return {} if row is None else self._meta(row)

    def put(self, key: str, meta: dict[str, object]) -> None:
        with self._connection() as conn:
            conn.execute("INSERT OR REPLACE INTO workspace_disks VALUES (?, ?, ?, ?, ?, ?)",
                         (key, str(meta.get("project", "")), str(meta.get("agent", "")),
                          json.dumps(list(cast(list[str], meta.get("bottles", [])))),
                          str(meta.get("owner", "")), str(meta.get("last_slug", ""))))
        self._chmod()

    def forget(self, key: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM workspace_disks WHERE key = ?", (key,))


def _store() -> WorkspaceDiskStore:
    from ...paths import host_db_path  # pylint: disable=import-outside-toplevel
    return WorkspaceDiskStore(host_db_path())


def _metas() -> dict[str, dict[str, object]]:
    """Every disk's row, by key: from the host DB in the host controller,
    else from it over its API, started if need be. Without its rows every
    disk would look orphaned to `cleanup workspaces`."""
    if host_access.in_host_controller():
        return _store().all()
    disks = host_access.ask(WORKSPACE_DISKS_PATH).get("disks")
    return ({str(k): v for k, v in disks.items() if isinstance(v, dict)}
            if isinstance(disks, dict) else {})


def _read_meta(disk: Path) -> dict[str, object]:
    if host_access.in_host_controller():
        return _store().get(disk.stem)
    return _metas().get(disk.stem, {})


def _write_meta(disk: Path, meta: dict[str, object]) -> None:
    if not host_access.in_host_controller():
        raise RuntimeError("only the host controller records workspace disks")
    _store().put(disk.stem, meta)


def _owner_running(owner: str) -> bool:
    """Whether the bottled agent `owner` still has a run dir: it's running,
    possibly under a host controller that restarted since (adoption)."""
    from . import util  # pylint: disable=import-outside-toplevel
    return bool(owner) and (util.env_dir() / "run" / owner).is_dir()


def in_use(disk: Path, meta: dict[str, object] | None = None) -> bool:
    """Whether a running bottle has `disk` attached."""
    return _owner_running(str((_read_meta(disk) if meta is None else meta).get("owner", "")))


def acquire(
    project: Path, agent: str, bottles: tuple[str, ...], *, slug: str, size: str,
) -> WorkspaceDisk | None:
    """The project's disk, made on first use and grown to `size`, held for
    `slug`; None (with a warning) when another bottle has it attached."""
    directory = disk_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{disk_key(project, agent, bottles)}.ext4"
    lock = open(path.with_suffix(".lock"), "a", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        warn(f"{project}'s workspace disk is in use by another bottle; copying it in instead")
        return None
    meta = _read_meta(path)
    owner = str(meta.get("owner", ""))
    if owner and owner != slug and _owner_running(owner):
        lock.close()
        warn(f"{project}'s workspace disk is attached to {owner}; copying it in instead")
        return None
    fresh = not path.exists()
    if fresh:
        _create(path, size)
    else:
        _grow(path, size)
    previous = "" if fresh else str(meta.get("last_slug", ""))
    meta.update({"project": str(project.resolve()), "agent": agent, "bottles": list(bottles),
                 "owner": slug, "last_slug": slug})
    _write_meta(path, meta)
    if fresh:
        info(f"made {project}'s workspace disk ({size}, sparse): {path}")
    else:
        info(f"using {project}'s workspace disk from its last run ({path})")
    return WorkspaceDisk(path=path, fresh=fresh, last_slug=previous, _lock=lock)


def _create(path: Path, size: str) -> None:
    proc = subprocess.run(["mke2fs", "-q", "-t", "ext4", "-F", str(path), size],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        die(f"creating the workspace disk {path} failed: {proc.stderr.strip()}")
    os.chmod(path, 0o600)


def _grow(path: Path, size: str) -> None:
    """Grow (never shrink) the disk to `size`; it isn't attached anywhere."""
    want = int(size[:-1]) * (1024 ** 3 if size.endswith("G") else 1024 ** 2)
    if path.stat().st_size >= want:
        return
    grown = subprocess.run(["truncate", "-s", size, str(path)],
                           capture_output=True, text=True, check=False)
    if grown.returncode == 0:
        grown = subprocess.run(["resize2fs", "-f", str(path)],
                               capture_output=True, text=True, check=False)
    if grown.returncode != 0:
        warn(f"couldn't grow the workspace disk {path} to {size}: {grown.stderr.strip()}")
    else:
        info(f"grew the workspace disk {path} to {size}")


def guest_path_ok(guest_path: str) -> bool:
    """Whether `guest_path` can go on the kernel command line as is."""
    return bool(_GUEST_PATH_RE.match(guest_path))


def boot_arg(guest_path: str) -> str:
    """The cmdline param the guest init mounts `/dev/vdc` at."""
    return f"bb_workspace={guest_path}"


def workspace_mode(disk: WorkspaceDisk | None, *, slug: str, home_kept: bool,
                   used_disk_last: bool) -> str:
    """How `provision_workspace` fills the workspace: `copy` (empty it, then
    copy), `resync` (keep only what the project ignores, then copy) or `keep`
    (the agent's own copy from its last run is there)."""
    if disk is None:
        # Its home's workspace dir was the disk's mount point: nothing in it.
        return "copy" if used_disk_last or not home_kept else "keep"
    if disk.fresh:
        return "copy"
    if home_kept and disk.last_slug == slug:
        return "keep"
    return "resync"


def listing() -> list[DiskInfo]:
    """Every workspace disk."""
    directory = disk_dir()
    if not directory.is_dir():
        return []
    metas = _metas()
    out = []
    for path in sorted(directory.glob("*.ext4")):
        meta = metas.get(path.stem, {})
        project = str(meta.get("project", ""))
        out.append(DiskInfo(path=path, project=project, bytes=path.stat().st_blocks * 512,
                            in_use=in_use(path, meta),
                            orphaned=not project or not Path(project).is_dir()))
    return out


def remove(path: Path) -> bool:
    """Delete a disk, its lock file and its row unless it's in use. True if
    removed. Outside the host controller, it does it (the row is its)."""
    if not host_access.in_host_controller():
        _check_disk_path(path)
        return host_access.ask(WORKSPACE_DISKS_REMOVE_PATH, {"key": path.stem}).get(
            "removed") is True
    return remove_here(path)


def _check_disk_path(path: Path) -> None:
    if path.parent != disk_dir() or path.suffix != ".ext4" or not _KEY_RE.match(path.stem):
        raise ValueError(f"{path} isn't a workspace disk")


def remove_here(path: Path) -> bool:
    """`remove`, in the host controller."""
    _check_disk_path(path)
    lock = open(path.with_suffix(".lock"), "a", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        if in_use(path, _store().get(path.stem)):
            return False
        path.unlink(missing_ok=True)
        _store().forget(path.stem)
        path.with_suffix(".lock").unlink(missing_ok=True)
        return True
    finally:
        lock.close()


__all__ = [
    "DEFAULT_SIZE", "STATE_MARKER", "DiskInfo", "WorkspaceDisk", "WorkspaceDiskStore", "acquire",
    "boot_arg", "disk_dir", "disk_key", "guest_path_ok", "in_use", "listing", "remove",
    "remove_here", "requested_size", "workspace_mode",
]
