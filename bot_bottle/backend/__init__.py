"""The bottle-backend package: abstract contract + backend selection.

Thin by design — nothing framework-heavy is imported at package init, so
importing any `backend.*` submodule (e.g. `backend.docker.util`) doesn't drag
the manifest / egress / git-gate framework into memory. The public names are
re-exported lazily:

  * the abstract contract (`BottleBackend`, `BottleSpec`, `Bottle`, …) lives in
    `backend.base`;
  * backend selection / enumeration (`get_bottle_backend`,
    `enumerate_active_agents`, …) in `backend.selection`;
  * the concrete backends and the freeze helpers in their own submodules.

`from bot_bottle.backend import X` resolves X on first access via `__getattr__`
and caches it at package level, so existing call-sites (and
`patch.object(backend_mod, X, …)`) keep working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import (
        ActiveAgent,
        BackendStatus,
        Bottle,
        BottleBackend,
        BottleCleanupPlan,
        BottleImages,
        BottlePlan,
        BottleSpec,
        EnumerationError,
        ExecResult,
    )
    from .selection import (
        enumerate_active_agents,
        get_bottle_backend,
        has_backend,
        is_backend_available,
        is_backend_ready,
        known_backend_names,
    )
    from .docker import DockerBottleBackend
    from .firecracker import FirecrackerBottleBackend
    from .macos_container import MacosContainerBottleBackend
    from .freeze import CommitCancelled, Freezer, get_freezer


# Public name -> submodule that defines it. Contract types resolve from `base`,
# selection/enumeration from `selection`, the concrete backends + freeze helpers
# from their own subpackages.
_LAZY_MODULES: dict[str, str] = {
    "BottleSpec": "base",
    "BottlePlan": "base",
    "BottleCleanupPlan": "base",
    "ExecResult": "base",
    "ActiveAgent": "base",
    "Bottle": "base",
    "BottleImages": "base",
    "BottleBackend": "base",
    "BackendStatus": "base",
    "EnumerationError": "base",
    "get_bottle_backend": "selection",
    "known_backend_names": "selection",
    "has_backend": "selection",
    "is_backend_available": "selection",
    "is_backend_ready": "selection",
    "enumerate_active_agents": "selection",
    "_print_vm_install_instructions": "selection",
    "DockerBottleBackend": "docker",
    "FirecrackerBottleBackend": "firecracker",
    "MacosContainerBottleBackend": "macos_container",
    "CommitCancelled": "freeze",
    "Freezer": "freeze",
    "get_freezer": "freeze",
}


def __getattr__(name: str) -> Any:
    """Lazily surface the package's public names from their submodules and
    cache them at package level — so `from bot_bottle.backend import X` and
    `patch.object(backend_mod, X, …)` keep working without importing the
    framework (or every backend) at package-init time."""
    mod = _LAZY_MODULES.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f"{__name__}.{mod}"), name)
    globals()[name] = value
    return value


__all__ = [
    "ActiveAgent",
    "BackendStatus",
    "Bottle",
    "BottleBackend",
    "BottleCleanupPlan",
    "BottleImages",
    "BottlePlan",
    "BottleSpec",
    "ExecResult",
    "EnumerationError",
    "CommitCancelled",
    "Freezer",
    "get_freezer",
    "DockerBottleBackend",
    "FirecrackerBottleBackend",
    "MacosContainerBottleBackend",
    "enumerate_active_agents",
    "get_bottle_backend",
    "has_backend",
    "is_backend_available",
    "is_backend_ready",
    "known_backend_names",
]
