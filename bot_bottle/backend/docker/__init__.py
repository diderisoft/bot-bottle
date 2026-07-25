"""Docker bottle backend.

The bulk of the implementation lives in sibling modules:

  - util:                 thin Docker subprocess wrappers
  - network:              Docker network plumbing
  - bottle_plan:          DockerBottlePlan
  - bottle_cleanup_plan:  DockerBottleCleanupPlan
  - bottle:               DockerBottle handle
  - prepare:              host-side resolution into a DockerBottlePlan
  - launch:               bring-up + teardown context manager
  - cleanup:              orphan enumeration, removal, active listing
  - backend:              DockerBottleBackend façade wiring the above
  - infra:                DockerInfraService (the per-host orchestrator + gateway pair)

Thin by design: the public names are re-exported lazily via `__getattr__`, so
importing a leaf like `backend.docker.util` doesn't drag `DockerBottleBackend`
(and the whole framework it pulls) into memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import DockerBottleBackend
    from .bottle import DockerBottle
    from .bottle_cleanup_plan import DockerBottleCleanupPlan
    from .bottle_plan import DockerBottlePlan


_LAZY_MODULES: dict[str, str] = {
    "DockerBottleBackend": "backend",
    "DockerBottle": "bottle",
    "DockerBottleCleanupPlan": "bottle_cleanup_plan",
    "DockerBottlePlan": "bottle_plan",
}


def __getattr__(name: str) -> Any:
    mod = _LAZY_MODULES.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{mod}"), name)
    globals()[name] = value
    return value


__all__ = [
    "DockerBottle",
    "DockerBottleBackend",
    "DockerBottleCleanupPlan",
    "DockerBottlePlan",
]
