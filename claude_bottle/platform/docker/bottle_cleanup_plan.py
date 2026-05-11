"""DockerBottleCleanupPlan — concrete subclass of BottleCleanupPlan.

Holds the tuples of container and network names that
DockerBottlePlatform.cleanup will remove. The y/N preflight reads
these via `print`; the CLI short-circuits via `empty`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ...log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class DockerBottleCleanupPlan(BottleCleanupPlan):
    """Resources DockerBottlePlatform.cleanup will remove. Produced by
    `prepare_cleanup` from a snapshot of `docker ps -a` + `docker
    network ls`; sorted so the y/N output is stable."""

    containers: tuple[str, ...]
    networks: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.containers and not self.networks

    def print(self) -> None:
        print(file=sys.stderr)
        for name in self.containers:
            info(f"container: {name}")
        for name in self.networks:
            info(f"network:   {name}")
        print(file=sys.stderr)
