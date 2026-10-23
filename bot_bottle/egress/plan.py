"""Egress launch DTOs.

`EgressRoute` (the host-side extension of the addon's wire `Route`) and
`EgressPlan` (the launch plan the backend contract references). Pure value
types — the route-building / rendering logic and the `Egress` service live in
`egress.service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..gateway.egress.types import Route


@dataclass(frozen=True)
class EgressRoute(Route):
    """Host-side extension of the addon's `Route`.

    Inherits `host`, `matches`, `auth_scheme`, and `token_env`
    from the gateway's wire `Route` — those are the fields that cross the
    YAML wire into the gateway. The fields below are host-only and
    are never serialised to the addon.

    `token_ref` is the host env var the CLI reads at launch and forwards
    into the container's environ under `token_env`.

    `roles` carries the manifest route's role tuple (reserved for
    future use; always empty today)."""

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
    # `audit.delivery` for this bottle, the host default filled in.
    audit_delivery: str = "best_effort"
