"""Cleanup plan for the macOS Apple Container backend."""

from __future__ import annotations

from dataclasses import dataclass

from ...log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class MacosContainerBottleCleanupPlan(BottleCleanupPlan):
    containers: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()

    def print(self) -> None:
        if not self.containers and not self.networks:
            info("macos-container cleanup: nothing to remove")
            return
        for name in self.containers:
            info(f"macos-container container: {name}")
        for name in self.networks:
            info(f"macos-container network: {name}")

    @property
    def empty(self) -> bool:
        return not self.containers and not self.networks
