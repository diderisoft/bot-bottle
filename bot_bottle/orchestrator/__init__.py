"""Per-host orchestrator service.

A single persistent per-host service that will run the gateway functions
(egress / git-gate / supervise), coordinate with the console, and broker
agent launches. This package is being built bottom-up, starting with the
backend-neutral "consolidation core" that needs no VM packaging:

  * `store.registry_store` — the SQLite runtime-state store + fail-closed
                      attribution (source IP + per-bottle identity token).
  * `service`       — the `OrchestratorCore`: owns the registry
                      (register/deregister), the supervise queue, attributes.
  * `server` — the HTTP control-plane RPC (register / deregister /
                      list / attribute / gateway / health), plus remote
                      launches relayed to the host controller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .store.registry_store import BottleRecord, RegistryStore, new_identity_token
    from .service import OrchestratorCore
    from .server import OrchestratorServer, create_app, make_server


# Facade name -> submodule that defines it. Lazy so importing a leaf (or the
# parent package, e.g. via `orchestrator.store.store_manager`) doesn't drag the
# whole orchestrator — server, http — while
# `from bot_bottle.orchestrator import RegistryStore` keeps working.
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
