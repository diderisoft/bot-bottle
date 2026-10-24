"""Cleanup plan for the Firecracker backend."""

from __future__ import annotations

from dataclasses import dataclass

from ...lib.log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class FirecrackerBottleCleanupPlan(BottleCleanupPlan):
    # Orphaned firecracker VMM pids and leftover per-bottle run dirs.
    vm_pids: tuple[int, ...] = ()
    run_dirs: tuple[str, ...] = ()

    def print(self) -> None:
        if self.empty:
            info("firecracker cleanup: nothing to remove")
            return
        for pid in self.vm_pids:
            info(f"firecracker VM process: pid {pid}")
        for path in self.run_dirs:
            info(f"firecracker run dir: {path}")

    @property
    def empty(self) -> bool:
        return not (self.vm_pids or self.run_dirs)

    def intersect(self, current: BottleCleanupPlan) -> "FirecrackerBottleCleanupPlan":
        if not isinstance(current, FirecrackerBottleCleanupPlan):
            raise TypeError("cleanup plans must have the same backend type")
        return FirecrackerBottleCleanupPlan(
            vm_pids=tuple(x for x in self.vm_pids if x in current.vm_pids),
            run_dirs=tuple(x for x in self.run_dirs if x in current.run_dirs),
        )
