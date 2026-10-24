"""Framework-neutral request policy for the egress addon: the git and route
decisions, testable without a proxy flow. The addon mutates flows and builds
responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .matching import (
    decide,
    decide_git_fetch,
    is_git_fetch_request,
    is_git_push_request,
)
from .types import LOG_FULL, Config, Route

GIT_PUSH_BLOCK_REASON = (
    "egress: git push over HTTPS is not supported; "
    "use the bottle.git SSH path (gitleaks-scanned by "
    "git-gate's pre-receive hook)."
)


@dataclass(frozen=True)
class RoutePolicyResult:
    """Flow mutations and outcome from route policy."""

    block_reason: str = ""
    block_code: str = ""
    strip_authorization: bool = False
    inject_authorization: str | None = None
    log_request: bool = False


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
    """Auth stripping, matching, injection and logging decisions."""
    strip_authorization = route is None or not route.preserve_auth
    effective_headers = {
        name.lower(): value
        for name, value in headers.items()
        if not (strip_authorization and name.lower() == "authorization")
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
        inject_authorization=decision.inject_authorization,
        log_request=config.log >= LOG_FULL,
    )


__all__ = [
    "GIT_PUSH_BLOCK_REASON",
    "RoutePolicyResult",
    "evaluate_route_policy",
    "git_block_reason",
]
