"""DockerBottleCleanupPlan — concrete subclass of BottleCleanupPlan.

Cleanup is centered on compose projects. `docker
compose ls` is the source of truth for what's running; the plan
carries the projects to `compose down`, plus three fallback buckets
for legacy / orphan resources:

  - stray_containers: pre-compose `bot-bottle-*` containers not
    attached to any compose project. Cleared via `docker rm -f`.
  - stray_networks: same idea for networks. Cleared via
    `docker network rm`.
  - orphan_state_dirs: per-bottle state dirs under
    ~/.bot-bottle/state/ that have no live compose project AND
    no `.preserve` marker. Reaped via `shutil.rmtree`.

Compose-managed networks are removed by `compose down --volumes`,
so they don't appear in stray_networks for a normal project — only
truly leftover ones.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ...lib.log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class DockerBottleCleanupPlan(BottleCleanupPlan):
    """Resources DockerBottleBackend.cleanup will remove. Produced by
    `prepare_cleanup`; sorted so the y/N output is stable."""

    projects: tuple[str, ...]
    stray_containers: tuple[str, ...]
    stray_networks: tuple[str, ...]
    orphan_state_dirs: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return (
            not self.projects
            and not self.stray_containers
            and not self.stray_networks
            and not self.orphan_state_dirs
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
            orphan_state_dirs=tuple(
                x for x in self.orphan_state_dirs if x in current.orphan_state_dirs
            ),
        )

    def print(self) -> None:
        print(file=sys.stderr)
        for name in self.projects:
            info(f"compose project: {name}")
        for name in self.stray_containers:
            info(f"stray container: {name}")
        for name in self.stray_networks:
            info(f"stray network:   {name}")
        for name in self.orphan_state_dirs:
            info(f"orphan state:    {name}")
        print(file=sys.stderr)
