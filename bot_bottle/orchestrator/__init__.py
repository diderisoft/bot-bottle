"""The per-host orchestrator: the control plane. `store.registry_store` is the
bottle registry and fail-closed attribution; `service` is `OrchestratorCore`;
`api` and `server` serve the HTTP control plane.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store.registry_store import BottleRecord, RegistryStore, new_identity_token
    from .service import OrchestratorCore
    from .server import OrchestratorServer, create_app, make_server


# Facade name -> defining submodule, resolved lazily.
_LAZY: dict[str, str] = {
    "BottleRecord": ".store.registry_store",
    "RegistryStore": ".store.registry_store",
    "new_identity_token": ".store.registry_store",
    "OrchestratorCore": ".service",
    "create_app": ".server",
    "OrchestratorServer": ".server",
    "make_server": ".server",
}


def __getattr__(name: str) -> Any:
    src = _LAZY.get(name)
    if src is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(src, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "BottleRecord",
    "RegistryStore",
    "new_identity_token",
    "OrchestratorCore",
    "create_app",
    "OrchestratorServer",
    "make_server",
]
