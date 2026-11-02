"""Live CPU % and memory used for a running bottle, by backend.

CPU is a delta between successive calls (returns 0.0 on the first call for
a given slug), as a share of the bottle's CPUs capped at 100; memory is a
point-in-time snapshot. `collect` returns None
when the backend is unsupported or the bottle isn't reachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BottleMetrics:
    cpu_pct: float
    mem_used: int  # bytes


# slug → (monotonic_time, total_jiffies) for CPU delta calculation
_cpu_cache: dict[str, tuple[float, int]] = {}


# --- Firecracker (Linux /proc) ---


def _proc_mem_bytes(pid: int) -> int:
    """VmRSS of `pid` in bytes (Firecracker mmaps guest memory so RSS ≈ guest
    RAM in use), or 0 on error."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return 0
    return 0


def _proc_jiffies(pid: int) -> int:
    """utime + stime for `pid` from /proc/PID/stat, 0 on error."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return 0
    # Format: pid (comm) state ... utime(14th) stime(15th) ...
    # comm can contain spaces; find the last ')' to skip it.
    try:
        idx = raw.rindex(")")
        fields = raw[idx + 1:].split()
        return int(fields[11]) + int(fields[12])
    except (ValueError, IndexError):
        return 0


def _delta_cpu_pct(slug: str, pid: int, cpus: int = 1) -> float:
    """CPU % of `cpus` cores since the last call for this slug (100 = all of
    them busy); 0.0 on the first call."""
    now = time.monotonic()
    jiffies = _proc_jiffies(pid)
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        hz = 100
    prev = _cpu_cache.get(slug)
    _cpu_cache[slug] = (now, jiffies)
    if prev is None:
        return 0.0
    elapsed = now - prev[0]
    if elapsed <= 0:
        return 0.0
    return min(100.0 * (jiffies - prev[1]) / hz / elapsed / max(cpus, 1), 100.0)


def _metrics_firecracker(slug: str, sample: str = "") -> BottleMetrics | None:
    # pylint: disable-next=import-outside-toplevel
    from ...backend.firecracker.cleanup import pid_for_slug, vcpus_for_slug
    pid = pid_for_slug(slug)
    if pid is None:
        return None
    key = f"{sample}:{slug}" if sample else slug  # each sampler its own CPU baseline
    # A share of the VM's vCPUs: one busy vCPU of two is 50%, not 100%.
    return BottleMetrics(cpu_pct=_delta_cpu_pct(key, pid, vcpus_for_slug(slug)),
                         mem_used=_proc_mem_bytes(pid))


# --- Docker and Apple Containers (stats CLI) ---


def _parse_size(s: str) -> int:
    """Parse a size string like '1.23GiB', '512MiB' → bytes; 0 on failure."""
    s = s.strip()
    for suffix, mult in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10),
                         ("GB", 10**9), ("MB", 10**6), ("KB", 10**3), ("B", 1)):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)]) * mult)
            except ValueError:
                return 0
    return 0


def _run_stats(argv: list[str]) -> dict[str, object] | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout.strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_stats(data: dict[str, object]) -> BottleMetrics:
    # Accept both Docker's ("CPUPerc", "MemUsage") and potential snake_case variants.
    try:
        cpu = float(str(data.get("CPUPerc") or data.get("cpu_percent") or "0").rstrip("%"))
    except ValueError:
        cpu = 0.0
    # `stats` counts 100 per host core; bottles set no CPU limit to scale by,
    # so cap it at 100 like the VMs' share.
    cpu = min(max(cpu, 0.0), 100.0)
    mem_raw = str(data.get("MemUsage") or data.get("memory_usage") or data.get("memory") or "")
    mem_used = _parse_size(mem_raw.split("/")[0]) if "/" in mem_raw else _parse_size(mem_raw)
    return BottleMetrics(cpu_pct=cpu, mem_used=mem_used)


def _metrics_docker(slug: str) -> BottleMetrics | None:
    from ...environment import resource_prefix  # pylint: disable=import-outside-toplevel
    name = f"{resource_prefix()}{slug}"
    data = _run_stats(["docker", "stats", "--no-stream", "--format", "json", name])
    return _parse_stats(data) if data is not None else None


def _metrics_macos_container(slug: str) -> BottleMetrics | None:
    from ...environment import resource_prefix  # pylint: disable=import-outside-toplevel
    name = f"{resource_prefix()}{slug}"
    data = _run_stats(["container", "stats", "--no-stream", "--format", "json", name])
    return _parse_stats(data) if data is not None else None


# ---


_COLLECTORS: dict[str, Callable[[str], BottleMetrics | None]] = {
    "firecracker": _metrics_firecracker,
    "docker": _metrics_docker,
    "macos_container": _metrics_macos_container,
}


def collect(slug: str, backend_name: str, *, sample: str = "") -> BottleMetrics | None:
    """Live CPU % and memory used for the running bottle `slug`, or None when
    the backend is unsupported or the bottle isn't reachable. `sample` names a
    sampler with its own CPU baseline (the console's live gauge samples every
    3 seconds, `host_stats` every minute)."""
    fn = _COLLECTORS.get(backend_name)
    if fn is None:
        return None
    return _metrics_firecracker(slug, sample) if fn is _metrics_firecracker else fn(slug)


__all__ = ["BottleMetrics", "collect"]
