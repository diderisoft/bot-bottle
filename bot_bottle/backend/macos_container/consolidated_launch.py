"""Consolidated bottle launch sequence for the macOS backend (PRD 0070).

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

The control plane and the gateway are one **infra container** here (see
`infra`), so `gateway_ip` and the control-plane host are the same address.

The consequence for the identity token: it is minted by registration, i.e.
*after* the agent container exists, so it cannot be baked into the run-time
env the way docker's compose spec does. It is delivered at `container exec`
time instead — see `bottle.MacosContainerBottle`.

That delivery is load-bearing, not a nicety: `/resolve` requires a matching
`(source_ip, identity_token)` pair and fail-closes with no source-IP-only
fallback (#366). So egress that does not carry the token is denied — which is
the safe direction, and is why the agent's init process is a bare `sleep` and
every real command arrives through `container exec`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...log import info
from ...orchestrator.client import OrchestratorClient, OrchestratorClientError
from ...orchestrator.registration import registration_inputs
from ..docker.gateway_provision import deprovision_git_gate, provision_git_gate
from . import util as container_mod
from .enumerate import CONTAINER_NAME_PREFIX, EnumerationError, enumerate_active
from .gateway import GATEWAY_NETWORK
from .gateway_provision import AppleGatewayTransport
from .infra import MacosInfraService, OrchestratorStartError


class ConsolidatedLaunchError(RuntimeError):
    """The consolidated register/provision sequence could not complete."""


@dataclass(frozen=True)
class GatewayEndpoint:
    """What the agent `container run` needs to reach the shared gateway (the
    infra container). `gateway_ip` is that container's host-only address, the
    same host the control-plane URL points at."""

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
    orchestrator_url: str


def ensure_gateway(
    *, service: MacosInfraService | None = None,
) -> GatewayEndpoint:
    """Ensure the per-host infra container (control plane + gateway) is up and
    report how to reach it. Idempotent — one singleton, so N bottle launches
    share it. Call before starting the agent container: the agent's proxy env
    needs `gateway_ip` at run time."""
    service = service or MacosInfraService()
    infra = service.ensure_running()
    return GatewayEndpoint(
        orchestrator_url=infra.control_plane_url,
        gateway_ip=infra.gateway_ip,
        gateway_ca_pem=service.ca_cert_pem(),
        network=service.network,
    )


def live_source_ips(network: str) -> list[str]:
    """Every running agent container's address on `network`.

    The reconciliation input: the orchestrator lives inside the infra
    container and cannot enumerate the host's containers, so the host has to
    tell it which bottles are actually up. Containers that have not been
    assigned an address yet contribute nothing — the reap's grace window, not
    this list, is what protects an in-flight launch.

    Raises `EnumerationError` when the live set cannot be determined
    authoritatively: either the container listing fails or any individual
    inspect fails. Callers must skip reconciliation in that case to avoid
    unregistering healthy bottles."""
    ips: list[str] = []
    for agent in enumerate_active():
        name = f"{CONTAINER_NAME_PREFIX}{agent.slug}"
        ip = container_mod.inspect_container_network_ip(name, network)
        if ip is None:
            raise EnumerationError(
                f"container inspect {name!r} failed; live set is not authoritative"
            )
        if ip:
            ips.append(ip)
    return ips


def register_agent(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    source_ip: str,
    endpoint: GatewayEndpoint,
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
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
    # which would resolve no policy at all and deny every host. Best-effort: a
    # reconciliation failure must not block an otherwise-fine launch.
    try:
        client.reconcile(live_source_ips(endpoint.network))
    except (OrchestratorClientError, EnumerationError) as e:
        info(f"registry reconciliation skipped: {e}")
    inputs = registration_inputs(egress_plan)
    reg = client.register_bottle(
        source_ip, image_ref=image_ref, policy=inputs.policy,
        metadata=inputs.metadata, tokens=tokens,
    )
    try:
        provision_git_gate(AppleGatewayTransport(), reg.bottle_id, git_gate_plan)
    except Exception:
        # Roll the registration back so a provisioning failure leaves no orphan.
        client.teardown_bottle(reg.bottle_id)
        raise
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        gateway_ip=endpoint.gateway_ip,
        network=endpoint.network,
        orchestrator_url=endpoint.orchestrator_url,
    )


def teardown_consolidated(bottle_id: str, *, orchestrator_url: str) -> None:
    """Deregister the bottle and remove its git-gate state from the gateway.
    Both steps are idempotent so this is safe from a cleanup trap. Does NOT
    stop the gateway — it's a persistent per-host singleton."""
    OrchestratorClient(orchestrator_url).teardown_bottle(bottle_id)
    deprovision_git_gate(AppleGatewayTransport(), bottle_id)


__all__ = [
    "GatewayEndpoint",
    "LaunchContext",
    "ensure_gateway",
    "live_source_ips",
    "register_agent",
    "teardown_consolidated",
    "ConsolidatedLaunchError",
    "OrchestratorStartError",
    "GATEWAY_NETWORK",
]
