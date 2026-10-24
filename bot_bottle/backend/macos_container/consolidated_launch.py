"""macOS register/teardown sequence.

Apple Container has no `--ip`: an agent's address comes from vmnet DHCP once
it's running. So the order is `ensure_gateway()`, the caller starts the agent
(its proxy env needs the gateway address), then `register_agent(source_ip)`.

The identity token is minted by registration, after the container exists, so it
is delivered at `container exec` time (see `bottle.MacosContainerBottle`). The
agent's init is a bare `sleep`; every real command comes through exec. Egress
without the token fails `/resolve`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...lib.log import info
from ...control.client import OrchestratorClient, OrchestratorClientError
from ..provision_bottle import deprovision_bottle, provision_bottle
from .gateway import GATEWAY_NETWORK
from .gateway_transport import MacosGatewayTransport
from .infra import MacosInfraService, OrchestratorStartError


class ConsolidatedLaunchError(RuntimeError):
    """The register/provision sequence couldn't complete."""


@dataclass(frozen=True)
class GatewayEndpoint:
    """What the agent's `container run` needs: the gateway's agent-network
    address and the orchestrator URL."""

    orchestrator_url: str
    gateway_ip: str  # proxy target
    gateway_ca_pem: str  # the CA to install
    network: str  # shared host-only network


@dataclass(frozen=True)
class LaunchContext:
    """What the running agent needs once registered."""

    bottle_id: str
    identity_token: str
    source_ip: str  # DHCP address (attribution key)
    gateway_ip: str
    network: str
    env_var_secret: str = ""  # key for the agent's env


def ensure_gateway(*, service: MacosInfraService) -> GatewayEndpoint:
    """Ensure the infra pair and return how to reach the gateway. Idempotent.
    Call before starting the agent."""
    orchestrator_url = service.ensure_running(launch_stages=True)
    endpoint = GatewayEndpoint(
        orchestrator_url=orchestrator_url,
        gateway_ip=service.gateway().address(),
        gateway_ca_pem=service.ca_cert_pem(),
        network=service.network,
    )
    return endpoint


def register_agent(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    source_ip: str,
    endpoint: GatewayEndpoint,
    tokens: dict[str, str] | None = None,
    env_var_secret: str | None = None,
) -> LaunchContext:
    """Register the running agent by `source_ip`, read from the live container,
    and provision its git-gate state. Raises on failure; the caller tears down."""
    client = OrchestratorClient(endpoint.orchestrator_url)
    # Reconcile first: a hard-killed launcher leaves a row with no container,
    # and a recycled vmnet address would make `/resolve` ambiguous and deny
    # everything. Best effort.
    try:
        client.reconcile()
    except OrchestratorClientError as e:
        info(f"registry reconciliation skipped: {e}")
    reg = provision_bottle(
        client, source_ip, egress_plan, git_gate_plan, MacosGatewayTransport(),
        tokens=tokens,
        env_var_secret=env_var_secret,
    )
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        gateway_ip=endpoint.gateway_ip,
        network=endpoint.network,
        env_var_secret=reg.env_var_secret,
    )


def deprovision_consolidated(
    bottle_id: str, *, service: MacosInfraService, timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent. The
    gateway keeps running."""
    deprovision_bottle(bottle_id, MacosGatewayTransport(),
                       orchestrator=service.orchestrator(),
                       timeout=timeout)


__all__ = [
    "GatewayEndpoint",
    "LaunchContext",
    "ensure_gateway",
    "register_agent",
    "deprovision_consolidated",
    "ConsolidatedLaunchError",
    "OrchestratorStartError",
    "GATEWAY_NETWORK",
]
