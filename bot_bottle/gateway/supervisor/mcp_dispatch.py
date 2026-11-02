"""Framework-neutral MCP method and tool dispatch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Protocol

from bot_bottle.gateway.egress.schema import load_config, route_to_yaml_dict
from bot_bottle.gateway.policy_resolver import PolicyResolveError, PolicyResolver
from bot_bottle.contract import supervise as _sv


class Request(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def params(self) -> dict[str, object]: ...


class MethodNotFoundError(Exception):
    """A JSON-RPC method with no MCP handler."""


class RouteResolutionError(Exception):
    """The caller's route table couldn't be resolved authoritatively."""


Handler = Callable[[dict[str, object]], object]


@dataclass(frozen=True)
class Handlers:
    initialize: Handler
    tools_list: Handler
    list_routes: Handler
    check_proposal: Handler
    get_environment: Handler
    propose: Handler


def dispatch(request: Request, handlers: Handlers) -> object:
    """Route one parsed request, independent of the HTTP server."""
    if request.method == "initialize":
        return handlers.initialize(request.params)
    if request.method == "notifications/initialized":
        return None
    if request.method == "tools/list":
        return handlers.tools_list(request.params)
    if request.method != "tools/call":
        raise MethodNotFoundError(request.method)

    tool = request.params.get("name")
    if tool == _sv.TOOL_LIST_EGRESS_ROUTES:
        return handlers.list_routes(request.params)
    if tool == _sv.TOOL_CHECK_PROPOSAL:
        return handlers.check_proposal(request.params)
    if tool == _sv.TOOL_GET_ENVIRONMENT:
        return handlers.get_environment(request.params)
    return handlers.propose(request.params)


def resolved_routes_payload(
    resolver: PolicyResolver,
    source_ip: str,
    identity_token: str,
) -> dict[str, object]:
    """The calling bottle's resolved route table."""
    try:
        policy, bottle_id, _tokens = resolver.resolve_policy_and_bottle_id(
            source_ip, identity_token,
        )
    except PolicyResolveError as exc:
        raise RouteResolutionError("orchestrator unavailable") from exc
    if not bottle_id:
        raise RouteResolutionError("request source is not attributed to a bottle")
    try:
        config = load_config(policy or "")
    except ValueError as exc:
        raise RouteResolutionError("resolved policy is invalid") from exc
    body = json.dumps(
        {"routes": [route_to_yaml_dict(route) for route in config.routes]},
        indent=2,
    )
    return {"content": [{"type": "text", "text": body}], "isError": False}


__all__ = [
    "Handlers",
    "MethodNotFoundError",
    "RouteResolutionError",
    "dispatch",
    "resolved_routes_payload",
]
