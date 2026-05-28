"""Docker bottle backend.

The bulk of the implementation lives in sibling modules:

  - util:                 thin Docker subprocess wrappers
  - network:              Docker network plumbing
  - pipelock:             DockerPipelockProxy lifecycle
  - bottle_plan:          DockerBottlePlan
  - bottle_cleanup_plan:  DockerBottleCleanupPlan
  - bottle:               DockerBottle handle
  - prepare:              host-side resolution into a DockerBottlePlan
  - launch:               bring-up + teardown context manager
  - cleanup:              orphan enumeration, removal, active listing
  - backend:              DockerBottleBackend façade wiring the above

This file only re-exports the public names so
`from bot_bottle.backend.docker import DockerBottleBackend` keeps
working.
"""

from __future__ import annotations

from .backend import DockerBottleBackend
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan

__all__ = [
    "DockerBottle",
    "DockerBottleBackend",
    "DockerBottleCleanupPlan",
    "DockerBottlePlan",
]
