"""Framework-neutral request policy for the egress addon: the git and route
decisions, testable without a proxy flow. The addon mutates flows and builds
responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence
from urllib.parse import unquote_plus

from .matching import (
    decide,
    decide_git_fetch,
    is_git_fetch_request,
    is_git_push_request,
)
from .types import LOG_FULL, Config, Route

GIT_PUSH_BLOCK_REASON = (
    "egress: git push doesn't go through egress, even to allowed hosts. "
    "Push to the bottle's own repos with their normal remote URLs: git "
    "rewrites them to git-gate, which scans the push for secrets. A repo "
    "the bottle doesn't have needs the operator to add it."
)

# Request positions an agent can carry a credential in. They're stripped on
# every route without `preserve_auth`, so the route's injected auth (always
# `Authorization`) is the only identity that goes upstream. This is a
# denylist and can't be complete. `Proxy-Authorization` and the identity
# header belong to the gateway and are stripped earlier. `Cookie` isn't here.
CREDENTIAL_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "private-token",
    "x-auth-token",
    "x-access-token",
    "x-gitea-token",
    "x-vault-token",
    "x-amz-security-token",
    "ocp-apim-subscription-key",
})
CREDENTIAL_QUERY_PARAMS = frozenset({
    "access_token",
    "token",
    "key",
    "api_key",
    "apikey",
    "private_token",
})


def strip_credential_query(path: str) -> str:
    """`path` without its credential query parameters. Names are compared
    decoded and case-insensitively; every other byte is kept."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    pairs = query.split("&")
    kept = [
        pair for pair in pairs
        if unquote_plus(pair.partition("=")[0]).strip().lower()
        not in CREDENTIAL_QUERY_PARAMS
    ]
    if len(kept) == len(pairs):
        return path
    return f"{base}?{'&'.join(kept)}" if kept else base


@dataclass(frozen=True)
class RoutePolicyResult:
    """Flow mutations and outcome from route policy."""

    block_reason: str = ""
    block_code: str = ""
    strip_authorization: bool = False
    # The headers stripped when `strip_authorization` (lowercased).
    strip_headers: frozenset[str] = frozenset()
    inject_authorization: str | None = None
    # The header `inject_authorization` goes in (lowercased).
    inject_header: str = "authorization"
    log_request: bool = False


def credential_headers(route: Route | None) -> frozenset[str]:
    """The agent headers stripped on `route`: `CREDENTIAL_HEADERS`, plus the
    header the route injects its auth into."""
    if route is not None and route.injects_auth:
        return CREDENTIAL_HEADERS | {route.injected_header}
    return CREDENTIAL_HEADERS


def git_block_reason(
    routes: Sequence[Route],
    host: str,
    request_path: str,
    query: str,
) -> str:
    """The HTTPS git denial reason, or ""."""
    if is_git_push_request(request_path, query):
        return GIT_PUSH_BLOCK_REASON
    if not is_git_fetch_request(request_path, query):
        return ""
    decision = decide_git_fetch(routes, host)
    return decision.reason if decision.action == "block" else ""


def evaluate_route_policy(
    config: Config,
    route: Route | None,
    *,
    host: str,
    request_path: str,
    method: str,
    headers: Mapping[str, str],
    env: Mapping[str, str],
) -> RoutePolicyResult:
    """Auth stripping, matching, injection and logging decisions.
    `strip_authorization` means every `strip_headers` header and
    `CREDENTIAL_QUERY_PARAMS` parameter goes; `matches` sees the headers
    without them, as they go upstream."""
    strip_authorization = route is None or not route.preserve_auth
    strip_headers = credential_headers(route) if strip_authorization else frozenset()
    effective_headers = {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() not in strip_headers
    }
    decision = decide(
        config.routes,
        host,
        request_path,
        env,
        request_method=method,
        request_headers=effective_headers,
        deny_reason=config.deny_reason,
    )
    return RoutePolicyResult(
        block_reason=decision.reason if decision.action == "block" else "",
        block_code=decision.code if decision.action == "block" else "",
        strip_authorization=strip_authorization,
        strip_headers=strip_headers,
        inject_authorization=decision.inject_authorization,
        inject_header=decision.inject_header,
        log_request=config.log >= LOG_FULL,
    )


__all__ = [
    "CREDENTIAL_HEADERS",
    "CREDENTIAL_QUERY_PARAMS",
    "GIT_PUSH_BLOCK_REASON",
    "RoutePolicyResult",
    "credential_headers",
    "evaluate_route_policy",
    "git_block_reason",
    "strip_credential_query",
]
