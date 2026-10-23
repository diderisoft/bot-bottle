"""Consolidated bottle launch sequence for the macOS backend.

The docker backend allocates a free address, pins the agent to it with
`--ip`, registers it, *then* starts the agent — registration precedes launch
because the pinned address is known up front.

**Apple Container 1.0.0 has no `--ip`.** The `--network` flag takes only
`<name>[,mac=…][,mtu=…]`; the address is assigned by vmnet's DHCP and is
knowable only once the container is running. So the macOS order inverts:

    ensure_gateway()  ->  caller starts the agent  ->  register_agent(source_ip)

That is why this module exposes two functions where docker has one — the
caller has to start the agent in between. `ensure_gateway` runs first because
the agent's proxy env needs the gateway's address at `container run` time; the
agent's *own* address (the attribution key) only exists afterwards.

The control plane and the gateway are **separate containers** here (see
`infra`): the orchestrator on the host-only control network, the gateway on the
agent network — `gateway_ip` is the gateway container's agent-network address,
distinct from the orchestrator's control-network host.

The consequence for the identity token: it is minted by registration, i.e.
*after* the agent container exists, so it cannot be baked into the run-time
env the way docker's compose spec does. It is delivered at `container exec`
time instead — see `bottle.MacosContainerBottle`.

That delivery is load-bearing, not a nicety: `/resolve` requires a matching
`(source_ip, identity_token)` pair and fail-closes with no source-IP-only
fallback. So egress that does not carry the token is denied — which is
the safe direction, and is why the agent's init process is a bare `sleep` and
every real command arrives through `container exec`.
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
    """The consolidated register/provision sequence could not complete."""


@dataclass(frozen=True)
class GatewayEndpoint:
    """What the agent `container run` needs to reach the shared gateway.
    `gateway_ip` is the gateway container's agent-network address (the agent's
    proxy target); `orchestrator_url` points at the separate control plane."""

    orchestrator_url: str
    gateway_ip: str         # the gateway's address — the agent's proxy target
    gateway_ca_pem: str     # the shared CA the provisioner installs
    network: str            # the shared host-only network to attach to


@dataclass(frozen=True)
class LaunchContext:
    """What the running agent needs once it has been registered."""

    bottle_id: str
    identity_token: str
    source_ip: str          # the agent's DHCP-assigned address (attribution key)
    gateway_ip: str
    network: str
    env_var_secret: str = ""  # encryption key injected into the agent's env


def ensure_gateway(*, service: MacosInfraService) -> GatewayEndpoint:
    """Ensure the per-host pair (orchestrator + gateway containers) is up and
    report how to reach the gateway. Idempotent — one singleton pair, so N bottle
    launches share it. Call before starting the agent container: the agent's
    proxy env needs `gateway_ip` at run time."""
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
    """Register the (already running) agent by its address and provision its
    git-gate state into the gateway. `source_ip` must be read from the live
    container — it is the attribution key the gateway resolves policy by.
    Raises on failure; the caller tears down."""
    client = OrchestratorClient(endpoint.orchestrator_url)
    # Self-heal before registering: a launcher that died hard (SIGKILL, closed
    # terminal, host sleep) never ran its teardown callback, leaving an active
    # row with no container. vmnet recycles addresses, so such a row can
    # collide with this bottle's — and `by_source_ip` fail-closes on ambiguity,
    # which would resolve no policy at all and deny every host. The
    # orchestrator asks the host controller which bottles are live. Best-effort:
    # a reconciliation failure must not block an otherwise-fine launch.
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
    """Deregister the bottle and remove its git-gate state from the gateway.
    Both steps are idempotent so this is safe from a cleanup trap. Does NOT
    stop the gateway — it's a persistent per-host singleton."""
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
