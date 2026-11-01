"""Host-level storage and memory statistics, sent to the console link."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


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


def collect(
    root: Path,
    *,
    max_disk_gb: int = 0,
    max_memory_gb: int = 0,
) -> dict[str, object]:
    """Snapshot of this host's storage and memory.

    *root* is the path whose filesystem is measured for disk usage (typically
    the bot-bottle state root). *max_disk_gb* and *max_memory_gb* are advisory
    caps in whole GiB; 0 means not configured.
    """
    disk_used, disk_total = _disk_stats(root)
    mem_used, mem_total = _memory_stats()
    return {
        "storage": {
            "used": disk_used,
            "total": disk_total,
            "cap": max_disk_gb * 1024 ** 3,
        },
        "memory": {
            "used": mem_used,
            "total": mem_total,
            "cap": max_memory_gb * 1024 ** 3,
        },
    }


__all__ = ["collect"]
