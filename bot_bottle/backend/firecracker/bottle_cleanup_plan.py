"""Cleanup plan for the Firecracker backend."""

from __future__ import annotations

from dataclasses import dataclass

from ...log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class FirecrackerBottleCleanupPlan(BottleCleanupPlan):
    # PIDs of orphaned firecracker VMM processes and the sidecar
    # containers left behind by previous bottles.
    vm_pids: tuple[int, ...] = ()
    containers: tuple[str, ...] = ()
    run_dirs: tuple[str, ...] = ()

    def print(self) -> None:
        if self.empty:
            info("firecracker cleanup: nothing to remove")
            return
        for pid in self.vm_pids:
            info(f"firecracker VM process: pid {pid}")
        for name in self.containers:
            info(f"firecracker sidecar container: {name}")
        for path in self.run_dirs:
            info(f"firecracker run dir: {path}")

    @property
    def empty(self) -> bool:
        return not (self.vm_pids or self.containers or self.run_dirs)
