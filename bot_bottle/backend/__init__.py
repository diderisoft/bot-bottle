"""The bottle-backend package: the abstract contract and backend selection.

Public names are re-exported lazily from `base`, `selection` and the backend
subpackages, so importing a `backend.*` submodule doesn't load the whole
framework.
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
        backend_os_requirement,
        docker_isolation_summary,
        enumerate_active_agents,
        environment_slice,
        get_bottle_backend,
        has_backend,
        is_backend_available,
        is_backend_ready,
        known_backend_names,
        launch_isolation_note,
        release_environment_slice,
    )
    from .docker import DockerBottleBackend
    from .firecracker import FirecrackerBottleBackend
    from .macos_container import MacosContainerBottleBackend
    from .freeze import CommitCancelled, Freezer, get_freezer


# Public name -> the submodule that defines it.
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
    "backend_os_requirement": "selection",
    "docker_isolation_summary": "selection",
    "get_bottle_backend": "selection",
    "known_backend_names": "selection",
    "launch_isolation_note": "selection",
    "has_backend": "selection",
    "is_backend_available": "selection",
    "is_backend_ready": "selection",
    "enumerate_active_agents": "selection",
    "environment_slice": "selection",
    "release_environment_slice": "selection",
    "_print_vm_install_instructions": "selection",
    "DockerBottleBackend": "docker",
    "FirecrackerBottleBackend": "firecracker",
    "MacosContainerBottleBackend": "macos_container",
    "CommitCancelled": "freeze",
    "Freezer": "freeze",
    "get_freezer": "freeze",
}


def __getattr__(name: str) -> Any:
    """Resolve a public name from its submodule on first access and cache it,
    so `from bot_bottle.backend import X` and `patch.object` both work."""
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
    "environment_slice",
    "get_bottle_backend",
    "has_backend",
    "is_backend_available",
    "is_backend_ready",
    "known_backend_names",
    "launch_isolation_note",
    "release_environment_slice",
    "backend_os_requirement",
    "docker_isolation_summary",
]
