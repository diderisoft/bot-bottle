"""Docker bottle platform.

The bulk of the implementation lives in sibling modules:

  - util:                 thin Docker subprocess wrappers
  - bottle_plan:          DockerBottlePlan
  - bottle_cleanup_plan:  DockerBottleCleanupPlan
  - bottle:               DockerBottle handle
  - platform:             DockerBottlePlatform

This file only re-exports the public names so
`from claude_bottle.bottles.docker import DockerBottlePlatform` keeps
working.
"""

from __future__ import annotations

from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan
from .platform import DockerBottlePlatform

__all__ = [
    "DockerBottle",
    "DockerBottleCleanupPlan",
    "DockerBottlePlan",
    "DockerBottlePlatform",
]
