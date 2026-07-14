"""Cleanup for the Firecracker backend.

Orphans are: firecracker VMM processes whose config lives under our run
dir, and the per-bottle run dirs. TAP slots free themselves (the flock
drops when the launcher exits), so there is nothing to reclaim there.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

from ...log import info
from . import util
from .bottle_cleanup_plan import FirecrackerBottleCleanupPlan


def _run_root() -> Path:
    return util.cache_dir() / "run"


def _orphan_vm_pids() -> list[int]:
    """firecracker processes whose --config-file is under our run dir."""
    run_root = str(_run_root())
    result = subprocess.run(
        ["pgrep", "-a", "firecracker"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or run_root not in parts[1]:
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return pids


def _run_dirs() -> list[str]:
    run_root = _run_root()
    if not run_root.is_dir():
        return []
    return sorted(str(p) for p in run_root.iterdir() if p.is_dir())


def prepare_cleanup() -> FirecrackerBottleCleanupPlan:
    return FirecrackerBottleCleanupPlan(
        vm_pids=tuple(_orphan_vm_pids()),
        run_dirs=tuple(_run_dirs()),
    )


def cleanup(plan: FirecrackerBottleCleanupPlan) -> None:
    for pid in plan.vm_pids:
        info(f"kill firecracker VM pid {pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for path in plan.run_dirs:
        info(f"rm -rf {path}")
        shutil.rmtree(path, ignore_errors=True)
