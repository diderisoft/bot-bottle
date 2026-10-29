"""The firecracker backend's cached images, for `bot-bottle cleanup images` (PRD 0088).

Under the cache dir's `rootfs/`:

  agent-ext4-<key>.ext4 (+ .ready, .smoked)  a prepared published agent base
  agent-<key>/, agent-overlay-<key>/         local and overlay agent builds
  <digest>-<role>-<init>/                    infra role trees (local infra builds)
  <digest>-<image>-<arch>/, other trees      publish and other base trees
  .staging-*                                 a build interrupted midway

plus downloaded `.ext4.gz` artifacts (the artifact cache) and the older
`infra-artifact/` downloads. The current pins' entries and the newest tree per
infra role stay; `infra/` (the live VMs' disks, keys and lock) is never listed.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from ...agent_image import resolve_agent_pin
from ...agent_provider import BUILTIN_HARNESSES
from ...contract.hostctl import InfraSettings
from ...paths import host_artifact_cache_dir
from ...published_artifacts import FORMAT_EXT4
from ..cache_entries import CacheEntry, disk_size, human, mtime, remove_paths
from . import image_builder, infra_rootfs, util

_PINNED = "a current pin"


def _pinned_ext4_paths(infra: InfraSettings) -> set[Path]:
    """The prepared agent bases this install's pins resolve to."""
    paths = set()
    for harness in BUILTIN_HARNESSES:
        with contextlib.suppress(Exception):  # no manifest, or not pinned here
            pin = resolve_agent_pin(harness, fmt=FORMAT_EXT4)
            if pin is not None:
                paths.add(image_builder._image_ext4_path(pin, infra))  # pylint: disable=protected-access
    return paths


def _pinned_digests(infra: InfraSettings) -> set[str]:
    """Artifact digests the current pins name (agent bases and infra roles)."""
    digests = set()
    for harness in BUILTIN_HARNESSES:
        with contextlib.suppress(Exception):
            pin = resolve_agent_pin(harness, fmt=FORMAT_EXT4)
            if pin is not None:
                digests.add(pin.digest)
    for role in infra_rootfs.ROLES:
        with contextlib.suppress(Exception):
            digests.add(infra_rootfs.role_artifact(role).digest)
    del infra
    return digests


def _infra_role(name: str) -> str | None:
    for role in infra_rootfs.ROLES:
        if f"-{role}-" in name:
            return role
    return None


def _rootfs_entries(infra: InfraSettings) -> list[CacheEntry]:
    rootfs = util.cache_dir() / "rootfs"
    if not rootfs.is_dir():
        return []
    pinned = _pinned_ext4_paths(infra)
    entries: list[CacheEntry] = []
    newest_role: dict[str, tuple[float, int]] = {}
    for path in sorted(rootfs.iterdir()):
        name = path.name
        if name.startswith("agent-ext4-") and name.endswith(".ext4") and path.is_file():
            ready, smoked = path.with_suffix(".ready"), path.with_suffix(".smoked")
            entries.append(CacheEntry(
                f"agent base {name}", disk_size(path), mtime(ready, mtime(path)),
                _PINNED if path in pinned else None, remove_paths(path, ready, smoked),
                build=False))
        elif (path.is_dir() and not name.startswith(".")) or name.startswith(".staging-"):
            role = _infra_role(name)
            kind = (f"{role} tree" if role else "agent build" if name.startswith("agent-")
                    else "interrupted build" if name.startswith(".staging-") else "base tree")
            used = mtime(path / ".bb-ready", mtime(path))
            entries.append(CacheEntry(f"{kind} {name}", disk_size(path), used, None,
                                      remove_paths(path)))
            if role and (role not in newest_role or used > newest_role[role][0]):
                newest_role[role] = (used, len(entries) - 1)
    for role, (_used, index) in newest_role.items():
        e = entries[index]
        entries[index] = CacheEntry(e.what, e.size, e.last_used, f"the current {role} tree",
                                    e.remove, e.build)
    return entries


def _download_entries(infra: InfraSettings) -> list[CacheEntry]:
    """Downloaded `.ext4.gz` artifacts, and the older `infra-artifact/` ones."""
    entries: list[CacheEntry] = []
    keep = {d.replace(":", "-") for d in _pinned_digests(infra)}
    root = host_artifact_cache_dir(infra.artifact_cache)
    for image_dir in sorted(root.iterdir()) if root.is_dir() else []:
        for digest_dir in sorted(image_dir.iterdir()) if image_dir.is_dir() else []:
            entries.append(CacheEntry(
                f"download {image_dir.name}/{digest_dir.name[:19]}", disk_size(digest_dir),
                mtime(digest_dir / ".verified", mtime(digest_dir)),
                _PINNED if digest_dir.name in keep else None, remove_paths(digest_dir),
                build=False))
    legacy = util.cache_dir() / "infra-artifact"
    for key_dir in sorted(legacy.iterdir()) if legacy.is_dir() else []:
        entries.append(CacheEntry(f"old infra download {key_dir.name}", disk_size(key_dir),
                                  mtime(key_dir), None, remove_paths(key_dir), build=False))
    return entries


def list_entries(infra: InfraSettings) -> list[CacheEntry]:
    return _rootfs_entries(infra) + _download_entries(infra)


def build_store() -> tuple[Path, bool]:
    """The buildah store volume, and whether a running orchestrator VM uses it."""
    from . import infra_vm  # pylint: disable=import-outside-toplevel
    from .orchestrator import build_store_volume_path  # pylint: disable=import-outside-toplevel
    return build_store_volume_path(), infra_vm._pidfile_alive(infra_vm._orch_dir())  # pylint: disable=protected-access


def prune_build_store(*, dry_run: bool) -> tuple[int, str] | None:
    """Delete the buildah store volume unless the orchestrator VM uses it (it is
    recreated on the next boot): (bytes freed, a line to report)."""
    path, in_use = build_store()
    if not path.exists():
        return None
    if in_use:
        return 0, "the buildah store is in use by the orchestrator VM; run `bot-bottle down` first"
    size = disk_size(path)
    if not dry_run:
        path.unlink(missing_ok=True)
    return size, f"{'would remove' if dry_run else 'removed'} the buildah store ({human(size)})"


__all__ = ["build_store", "list_entries", "prune_build_store"]
