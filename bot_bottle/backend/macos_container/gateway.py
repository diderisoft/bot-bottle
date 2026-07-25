"""Shared network/image constants for the macOS consolidated infra container.

The gateway data plane no longer runs as its own Apple container — it shares a
single per-host **infra container** with the control plane (see `infra`),
because two Apple-Container guests writing one `bot-bottle.db` over virtiofs
would race incoherent `fcntl` locks. This module holds the pieces both the
infra service and the launch/provision glue need: the network names, the
gateway image, and the network-creation helper.
"""

from __future__ import annotations

import os

from ...gateway import GatewayError
from . import util as container_mod

# The shared host-only network the infra container and every agent bottle sit
# on. The agent's address here is the attribution key. Distinct from the docker
# names so both backends can coexist on one host.
GATEWAY_NETWORK = "bot-bottle-mac-gateway"
# The NAT network that gives the gateway (and only it) a route out.
GATEWAY_EGRESS_NETWORK = "bot-bottle-mac-egress"
# The control network the gateway reaches the orchestrator over (host-only).
# Only the orchestrator + gateway join it; agents never do, so agents have no
# route to the control plane (PRD 0070 "Separating the planes").
CONTROL_NETWORK = "bot-bottle-mac-control"

GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")
ORCHESTRATOR_IMAGE = os.environ.get(
    "BOT_BOTTLE_ORCHESTRATOR_IMAGE", "bot-bottle-orchestrator:latest"
)

DEFAULT_CA_TIMEOUT_SECONDS = 30.0


def ensure_networks(
    network: str = GATEWAY_NETWORK,
    egress_network: str = GATEWAY_EGRESS_NETWORK,
    control_network: str = CONTROL_NETWORK,
) -> None:
    """Create the shared host-only agent network, the NAT egress network, and
    the host-only control network. Idempotent — `create_network` tolerates
    'already exists'."""
    container_mod.create_network(egress_network)
    container_mod.create_network(network, internal=True)
    container_mod.create_network(control_network, internal=True)


__all__ = [
    "GATEWAY_NETWORK",
    "GATEWAY_EGRESS_NETWORK",
    "CONTROL_NETWORK",
    "GATEWAY_IMAGE",
    "ORCHESTRATOR_IMAGE",
    "GatewayError",
    "DEFAULT_CA_TIMEOUT_SECONDS",
    "ensure_networks",
]
