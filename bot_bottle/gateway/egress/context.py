"""Fail-closed resolution of a client's policy and egress credentials."""

from __future__ import annotations

import typing

from ...log import debug
from .types import Config


DENY_UNATTRIBUTED = (
    "egress: this request was not attributed to any bottle, so no egress policy "
    "applies and every host is denied. Either the bottle's registry row is "
    "missing/ambiguous (torn down, or another bottle claimed its source IP), or "
    "the request carried no matching identity token — check that the caller's "
    "proxy URL includes it. This is not an allowlist problem."
)
DENY_UNPARSEABLE = (
    "egress: this bottle's egress policy could not be parsed, so it is being "
    "treated as deny-all. Fix the bottle's egress.routes; every host is denied "
    "until it loads."
)
DENY_RESOLVER_ERROR = (
    "egress: the orchestrator could not be reached to resolve this bottle's "
    "egress policy, so every host is denied (fail-closed). Check that the "
    "control plane is up; this is not an allowlist problem."
)


class PolicyResolverLike(typing.Protocol):
    def resolve(self, source_ip: str, identity_token: str = ...) -> str | None: ...


class ContextResolverLike(typing.Protocol):
    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = ...,
    ) -> tuple[str | None, str | None, dict[str, str]]: ...


def _config_from_policy(policy: str | None) -> Config:
    # Local import keeps schema parsing independent of resolver protocols.
    from .schema import load_config
    if not policy:
        return Config(routes=(), deny_reason=DENY_UNATTRIBUTED)
    try:
        return load_config(policy)
    except ValueError:
        return Config(routes=(), deny_reason=DENY_UNPARSEABLE)


def resolve_client_config(
    resolver: PolicyResolverLike, client_ip: str, identity_token: str = "",
) -> Config:
    try:
        policy = resolver.resolve(client_ip, identity_token)
    except Exception as exc:  # noqa: BLE001 - a policy lookup failure must deny
        debug(
            "egress policy resolution failed; applying deny-all",
            context={"error_type": type(exc).__name__},
        )
        return Config(routes=(), deny_reason=DENY_RESOLVER_ERROR)
    return _config_from_policy(policy)


def resolve_client_context(
    resolver: ContextResolverLike, client_ip: str, identity_token: str = "",
) -> tuple[Config, str, dict[str, str]]:
    try:
        policy, bottle_id, tokens = resolver.resolve_policy_and_bottle_id(
            client_ip, identity_token)
    except Exception as exc:  # noqa: BLE001 - a policy lookup failure must deny
        debug(
            "egress context resolution failed; applying deny-all",
            context={"error_type": type(exc).__name__},
        )
        return Config(routes=(), deny_reason=DENY_RESOLVER_ERROR), "", {}
    return _config_from_policy(policy), (bottle_id or ""), tokens
