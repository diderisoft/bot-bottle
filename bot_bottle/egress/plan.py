"""Egress launch types: `EgressRoute` and `EgressPlan`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..gateway.egress.types import Route


@dataclass(frozen=True)
class EgressRoute(Route):
    """Host-side extension of the wire `Route`; the fields here never reach the
    gateway. `token_ref` is the host secret forwarded under `token_env`.
    `roles` is reserved and always empty."""

    token_ref: str = ""
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class EgressPlan:
    slug: str
    routes_path: Path
    routes: tuple[EgressRoute, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""
    mitmproxy_ca_host_path: Path = Path()
    mitmproxy_ca_cert_only_host_path: Path = Path()
    log: int = 0
    canary: str = ""
    canary_env: str = ""
    # `audit.delivery`, defaulted from the host.
    audit_delivery: str = "best_effort"
    # `audit.firehose`: `full`, or `metadata` to keep bodies and headers out.
    firehose: str = "full"
