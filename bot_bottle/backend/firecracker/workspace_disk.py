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
from typing import IO

from ...lib.log import die, info, warn
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


def _meta_path(disk: Path) -> Path:
    return disk.with_suffix(".json")


def _read_meta(disk: Path) -> dict[str, object]:
    try:
        raw = json.loads(_meta_path(disk).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_meta(disk: Path, meta: dict[str, object]) -> None:
    path = _meta_path(disk)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _owner_running(owner: str) -> bool:
    """Whether the bottled agent `owner` still has a run dir: it's running,
    possibly under a host controller that restarted since (adoption)."""
    from . import util  # pylint: disable=import-outside-toplevel
    return bool(owner) and (util.env_dir() / "run" / owner).is_dir()


def in_use(disk: Path) -> bool:
    """Whether a running bottle has `disk` attached."""
    return _owner_running(str(_read_meta(disk).get("owner", "")))


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
    out = []
    for path in sorted(directory.glob("*.ext4")):
        project = str(_read_meta(path).get("project", ""))
        out.append(DiskInfo(path=path, project=project, bytes=path.stat().st_blocks * 512,
                            in_use=in_use(path),
                            orphaned=not project or not Path(project).is_dir()))
    return out


def remove(path: Path) -> bool:
    """Delete a disk and its files unless it's in use. True if removed."""
    lock = open(path.with_suffix(".lock"), "a", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        if in_use(path):
            return False
        for p in (path, _meta_path(path)):
            p.unlink(missing_ok=True)
        path.with_suffix(".lock").unlink(missing_ok=True)
        return True
    finally:
        lock.close()


__all__ = [
    "DEFAULT_SIZE", "STATE_MARKER", "DiskInfo", "WorkspaceDisk", "acquire", "boot_arg", "disk_dir",
    "disk_key", "guest_path_ok", "in_use", "listing", "remove", "requested_size",
    "workspace_mode",
]
