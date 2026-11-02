"""Host-level storage and memory statistics, and the bottles' budget
(`budget.py`), sent to the console link as `host_stats`."""

from __future__ import annotations

import os
import platform
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ... import forge_alerts
from ...lib.config_store import HostBudget
from . import budget

if TYPE_CHECKING:
    from .bottles import BottleService

GIB = 1024 ** 3


def _disk_stats(root: Path) -> tuple[int, int]:
    """(used_bytes, total_bytes) for the filesystem that contains *root*."""
    try:
        sv = os.statvfs(root)
    except OSError:
        return 0, 0
    total = sv.f_frsize * sv.f_blocks
    free = sv.f_frsize * sv.f_bavail
    return total - free, total


def _memory_stats_linux() -> tuple[int, int]:
    """(used_bytes, total_bytes) from /proc/meminfo."""
    fields: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, raw = line.partition(":")
                fields[key.strip()] = int(raw.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return 0, 0
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", 0)
    return total - available, total


def _memory_stats_macos() -> tuple[int, int]:
    """(used_bytes, total_bytes) on macOS via sysctl and vm_stat."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, check=False,
        )
        total = int(out.stdout.strip()) if out.returncode == 0 else 0
    except (OSError, ValueError):
        total = 0

    available = 0
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, check=False)
        page_size = 4096
        free_pages = 0
        for line in out.stdout.splitlines():
            if "page size of" in line:
                try:
                    page_size = int(line.split("page size of")[1].split()[0])
                except (IndexError, ValueError):
                    pass
            for key in ("Pages free", "Pages inactive"):
                if line.startswith(key + ":"):
                    try:
                        free_pages += int(line.split(":")[1].strip().rstrip("."))
                    except (IndexError, ValueError):
                        pass
        available = free_pages * page_size
    except OSError:
        pass

    return total - available, total


def _memory_stats() -> tuple[int, int]:
    if platform.system() == "Darwin":
        return _memory_stats_macos()
    return _memory_stats_linux()


def _existing(path: Path) -> Path | None:
    """`path`, or its nearest existing parent: a cache dir that isn't made
    yet still says which filesystem it will fill."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _storage(paths: Sequence[Path]) -> tuple[int, int]:
    """(used, total) bytes over the filesystems holding `paths`, each
    filesystem counted once (the state root and the caches are often one)."""
    used = total = 0
    seen: set[int] = set()
    for path in paths:
        where = _existing(path)
        if where is None:
            continue
        try:
            device = os.stat(where).st_dev
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        u, t = _disk_stats(where)
        used, total = used + u, total + t
    return used, total


def collect(paths: Sequence[Path], *, limits: HostBudget = HostBudget()) -> dict[str, object]:
    """Snapshot of this host's storage (over the filesystems holding `paths`)
    and memory, host-wide, each with the budget in bytes as `cap` (0 = none
    set; kept for consoles that predate the `budget` block)."""
    disk_used, disk_total = _storage(paths)
    mem_used, mem_total = _memory_stats()
    return {
        "storage": {"used": disk_used, "total": disk_total, "cap": limits.disk_gb * GIB},
        "memory": {"used": mem_used, "total": mem_total, "cap": limits.memory_gb * GIB},
    }


def stats_paths() -> tuple[Path, ...]:
    """Where bot-bottle's disk use lives: the state root (bottles' state,
    transcripts, the host DB) and the caches (images, VM disks)."""
    return tuple(path for _, path in budget.storage_roots())


def _os_name() -> str:
    """The host's OS as people call it: macOS, or a Linux's distribution name
    (`NAME` in /etc/os-release, e.g. NixOS), else `platform.system()`."""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Linux":
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                for line in fh:
                    key, _, value = line.strip().partition("=")
                    if key == "NAME" and value.strip('"'):
                        return value.strip('"')
        except OSError:
            pass
    return system


def about() -> dict[str, str]:
    """What the host's page says about it: its OS, the backend its last
    bring-up recorded ("" before one) and this install's version."""
    # pylint: disable=import-outside-toplevel
    from ...engine import install_version
    from ..client import recorded_orchestrator
    endpoint = recorded_orchestrator()
    version, commit = install_version()
    return {"os": _os_name(), "backend": endpoint.backend if endpoint else "",
            "version": version, "commit": commit}


def snapshot(bottles: "BottleService | None" = None) -> dict[str, object]:
    """What the console link sends as `host_stats`: the host-wide numbers,
    the bottles' `budget`, `about` the host and its `forge_alerts` (forge
    tokens that stopped working), read fresh each time so a budget set while
    the host controller runs shows at the next push."""
    limits = budget.host_budget()
    running = bottles.allocation() if bottles is not None else []
    return {**collect(stats_paths(), limits=limits),
            "budget": budget.block(running, limits, budget.METER.current()),
            "about": about(), "forge_alerts": forge_alerts.current()}


__all__ = ["about", "collect", "snapshot", "stats_paths"]
