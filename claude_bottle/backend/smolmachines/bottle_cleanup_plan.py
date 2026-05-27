"""SmolmachinesBottleCleanupPlan — concrete BottleCleanupPlan (issue #77).

Tracks the resources `SmolmachinesBottleBackend.cleanup` will
remove:

  - machines:  smolvm machines whose name starts with
               `claude-bottle-` (running or stopped). Stopped +
               deleted via `smolvm machine stop` + `machine delete -f`.
  - bundles:   docker containers `claude-bottle-sidecars-<slug>`
               left over from a smolmachines bottle (the bundle's
               port-forwards stay published on lo0 aliases until
               the container is gone). Removed via `docker rm -f`.
  - networks:  docker networks `claude-bottle-bundle-<slug>`
               attached to the bundles. Removed via
               `docker network rm`.

Smolmachines state dirs live under the same `~/.claude-bottle/state/`
path the docker backend uses; the docker backend's
`prepare_cleanup` already enumerates orphan state dirs and is the
single source of truth for that bucket (consults
`enumerate_active_bottles()` so it doesn't reap a live
smolmachines bottle's dir)."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ...log import info
from .. import BottleCleanupPlan


@dataclass(frozen=True)
class SmolmachinesBottleCleanupPlan(BottleCleanupPlan):
    """Resources SmolmachinesBottleBackend.cleanup will remove.
    Produced by `prepare_cleanup`; sorted so the y/N output is
    stable."""

    machines: tuple[str, ...] = ()
    bundles: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.machines and not self.bundles and not self.networks

    def print(self) -> None:
        print(file=sys.stderr)
        for name in self.machines:
            info(f"smolvm machine:  {name}")
        for name in self.bundles:
            info(f"bundle container:{name}")
        for name in self.networks:
            info(f"bundle network:  {name}")
        print(file=sys.stderr)
