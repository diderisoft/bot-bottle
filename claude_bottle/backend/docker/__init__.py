"""Docker bottle backend.

The bulk of the implementation lives in sibling modules:

  - util:                 thin Docker subprocess wrappers
  - network:              Docker network plumbing
  - bottle_plan:          DockerBottlePlan
  - bottle_cleanup_plan:  DockerBottleCleanupPlan
  - bottle:               DockerBottle handle
  - backend:              DockerBottleBackend

This file only re-exports the public names so
`from claude_bottle.backend.docker import DockerBottleBackend` keeps
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
