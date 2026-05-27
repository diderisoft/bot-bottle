"""SmolmachinesBottleCleanupPlan — concrete BottleCleanupPlan stub
(PRD 0023 chunk 1).

Chunk 1 always reports nothing-to-clean. Real enumeration —
orphaned smolvm machines, stranded gvproxy sockets, leftover
sidecar bundle containers — lands in chunk 4 alongside the
integration-test sweep that exercises teardown."""

from __future__ import annotations

from dataclasses import dataclass

from ...log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class SmolmachinesBottleCleanupPlan(BottleCleanupPlan):
    def print(self) -> None:
        info("smolmachines cleanup: nothing to remove (chunk 4 will "
             "enumerate orphan machines + gvproxy sockets)")

    @property
    def empty(self) -> bool:
        return True
