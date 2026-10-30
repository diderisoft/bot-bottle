"""`DockerBottleCleanupPlan`: compose projects to `compose down`, plus
`bot-bottle-*` containers and networks not owned by any compose project.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ...lib.log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class DockerBottleCleanupPlan(BottleCleanupPlan):
    """What `cleanup` will remove, sorted for stable y/N output."""

    projects: tuple[str, ...]
    stray_containers: tuple[str, ...]
    stray_networks: tuple[str, ...]
    # Bottled agents' homes whose state isn't kept for resume.
    volumes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return (
            not self.projects
            and not self.stray_containers
            and not self.stray_networks
            and not self.volumes
        )

    def intersect(self, current: BottleCleanupPlan) -> "DockerBottleCleanupPlan":
        if not isinstance(current, DockerBottleCleanupPlan):
            raise TypeError("cleanup plans must have the same backend type")
        return DockerBottleCleanupPlan(
            projects=tuple(x for x in self.projects if x in current.projects),
            stray_containers=tuple(
                x for x in self.stray_containers if x in current.stray_containers
            ),
            stray_networks=tuple(
                x for x in self.stray_networks if x in current.stray_networks
            ),
            volumes=tuple(x for x in self.volumes if x in current.volumes),
        )

    def print(self) -> None:
        print(file=sys.stderr)
        for name in self.projects:
            info(f"compose project: {name}")
        for name in self.stray_containers:
            info(f"stray container: {name}")
        for name in self.stray_networks:
            info(f"stray network:   {name}")
        for name in self.volumes:
            info(f"home volume:     {name}")
        print(file=sys.stderr)
