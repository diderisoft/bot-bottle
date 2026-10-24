"""Host side of egress: `service` (the `Egress` service and route rendering)
and `plan` (`EgressPlan`, `EgressRoute`). Enforcement is in
`bot_bottle.gateway.egress`. Names are re-exported lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .plan import EgressPlan, EgressRoute
    from .service import (
        CLAUDE_HOST_CREDENTIAL_TOKEN_REF,
        CODEX_HOST_CREDENTIAL_TOKEN_REF,
        EGRESS_HOSTNAME,
        EGRESS_ROUTES_FILENAME,
        EGRESS_ROUTES_IN_CONTAINER,
        Egress,
        egress_agent_env_entries,
        egress_gateway_env_entries,
        egress_manifest_routes,
        egress_render_routes,
        egress_resolve_token_values,
        egress_routes_for_bottle,
        egress_token_env_map,
    )


_LAZY: dict[str, str] = {
    "EgressPlan": ".plan",
    "EgressRoute": ".plan",
    "Egress": ".service",
    "CLAUDE_HOST_CREDENTIAL_TOKEN_REF": ".service",
    "CODEX_HOST_CREDENTIAL_TOKEN_REF": ".service",
    "EGRESS_HOSTNAME": ".service",
    "EGRESS_ROUTES_FILENAME": ".service",
    "EGRESS_ROUTES_IN_CONTAINER": ".service",
    "egress_agent_env_entries": ".service",
    "egress_gateway_env_entries": ".service",
    "egress_manifest_routes": ".service",
    "egress_render_routes": ".service",
    "egress_resolve_token_values": ".service",
    "egress_routes_for_bottle": ".service",
    "egress_token_env_map": ".service",
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
    "CLAUDE_HOST_CREDENTIAL_TOKEN_REF",
    "CODEX_HOST_CREDENTIAL_TOKEN_REF",
    "EGRESS_HOSTNAME",
    "EGRESS_ROUTES_FILENAME",
    "EGRESS_ROUTES_IN_CONTAINER",
    "Egress",
    "EgressPlan",
    "EgressRoute",
    "egress_manifest_routes",
    "egress_render_routes",
    "egress_resolve_token_values",
    "egress_routes_for_bottle",
    "egress_agent_env_entries",
    "egress_gateway_env_entries",
    "egress_token_env_map",
]
