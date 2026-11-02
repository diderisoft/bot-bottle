"""Route matching and request-policy decisions for the egress gateway."""

from __future__ import annotations

import typing

from ...contract import blocks
from .types import Decision, MatchEntry, PathMatch, Route


def _path_matches(pm: PathMatch, request_path: str) -> bool:
    if pm.type == "exact":
        return request_path == pm.value
    if pm.type == "prefix":
        if request_path == pm.value:
            return True
        if not pm.value.endswith("/"):
            return request_path.startswith(pm.value + "/")
        return request_path.startswith(pm.value)
    return (
        pm.type == "regex"
        and pm.compiled is not None
        and pm.compiled.search(request_path) is not None
    )


def _entry_matches(
    entry: MatchEntry, request_path: str, request_method: str,
    request_headers: typing.Mapping[str, str],
) -> bool:
    if entry.paths and not any(_path_matches(pm, request_path) for pm in entry.paths):
        return False
    if entry.methods and request_method.upper() not in entry.methods:
        return False
    for match in entry.headers:
        value = request_headers.get(match.name.lower())
        if value is None:
            return False
        if match.type == "exact" and value != match.value:
            return False
        if match.type == "regex" and (
            match.compiled is None or match.compiled.search(value) is None
        ):
            return False
    return True


def evaluate_matches(
    route: Route, request_path: str, request_method: str = "GET",
    request_headers: typing.Mapping[str, str] | None = None,
) -> bool:
    """Return whether a request satisfies a route's optional match entries."""
    if not route.matches:
        return True
    return any(_entry_matches(entry, request_path, request_method, request_headers or {})
               for entry in route.matches)


def is_git_push_request(path: str, query: str) -> bool:
    return path.endswith("/git-receive-pack") or (
        path.endswith("/info/refs") and any(
            pair.partition("=") == ("service", "=", "git-receive-pack")
            for pair in query.split("&")
        )
    )


def is_git_fetch_request(path: str, query: str) -> bool:
    return path.endswith("/git-upload-pack") or (
        path.endswith("/info/refs") and any(
            pair.partition("=") == ("service", "=", "git-upload-pack")
            for pair in query.split("&")
        )
    )


def match_route(routes: typing.Sequence[Route], request_host: str) -> Route | None:
    target = request_host.lower()
    return next((route for route in routes if route.host.lower() == target), None)


def decide(
    routes: typing.Sequence[Route], request_host: str, request_path: str,
    environ: typing.Mapping[str, str], *, request_method: str = "GET",
    request_headers: typing.Mapping[str, str] | None = None, deny_reason: str = "",
) -> Decision:
    route = match_route(routes, request_host)
    if route is None:
        return Decision("block", deny_reason or (
            f"egress: host {request_host!r} is not in the bottle's egress.routes "
            "allowlist. Declare a route for it or remove the request."),
            code=blocks.CONFIG if deny_reason else blocks.ALLOWLIST)
    if not evaluate_matches(route, request_path, request_method, request_headers):
        return Decision("block", (
            f"egress: request {request_method} {request_path!r} does not match any "
            f"entry in matches for {route.host!r}"), code=blocks.ROUTE)
    # Inspected routes without gateway-injected auth default to read-only
    # (GET/HEAD) when no matches are declared. To allow writes, add a matches
    # entry. inspect: false (passthrough) routes are excluded: the operator
    # explicitly opted out of gateway interception for those hosts.
    if route.inspect and not route.injects_auth and not route.preserve_auth and not route.matches:
        method_upper = request_method.upper()
        if method_upper not in ("GET", "HEAD"):
            return Decision("block", (
                f"egress: {method_upper} {request_path!r} on {route.host!r}: routes "
                "without gateway auth default to read-only (GET/HEAD); add a matches "
                "entry to allow writes explicitly."), code=blocks.ROUTE)
    if route.injects_auth:
        token = environ.get(route.token_env, "")
        if not token:
            return Decision("block", (
                f"egress: route for {route.host!r} declared auth but env var "
                f"{route.token_env!r} is unset"), code=blocks.CONFIG)
        return Decision("forward", inject_authorization=route.auth_value(token),
                        inject_header=route.injected_header)
    return Decision("forward")


def decide_git_fetch(routes: typing.Sequence[Route], request_host: str) -> Decision:
    route = match_route(routes, request_host)
    if route is not None and route.git_fetch:
        return Decision("forward")
    return Decision("block", (
        "egress: git fetch/clone over HTTPS is not allowed by default; use git-gate "
        "for declared repos or set egress.routes[].git.fetch=true for explicit "
        "read-only HTTPS Git access."), code=blocks.ROUTE)
