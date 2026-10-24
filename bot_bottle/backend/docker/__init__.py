"""Docker bottle backend. Public names are re-exported lazily so importing a
leaf module stays cheap.
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
