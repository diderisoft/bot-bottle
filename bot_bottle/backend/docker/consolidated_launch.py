"""Docker register/teardown sequence: ensure the infra pair, create the
bottle's own agent network with the gateway on it, register the bottle at its
pinned source IP and provision its git-gate state. Returns the agent's
`LaunchContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import run_docker
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...control.client import OrchestratorClient
from .infra import INFRA_NAME, DockerInfraService
from ..provision_bottle import deprovision_bottle, provision_bottle
from .agent_network import (
    connect_gateway, create_agent_network, remove_agent_network,
)
from .gateway_transport import DockerGatewayTransport
from .gateway_net import agent_subnet_addresses


class ConsolidatedLaunchError(RuntimeError):
    """The register/provision sequence couldn't complete."""


@dataclass(frozen=True)
class LaunchContext:
    """What the agent container needs to join its network."""

    bottle_id: str
    identity_token: str
    source_ip: str  # pinned address (attribution key)
    network: str  # the bottle's own agent network
    gateway_ip: str  # proxy target: the gateway's address on that network
    env_var_secret: str = ""  # key for the agent's env
    # Other registered bottles' addresses, for the isolation probe.
    peer_ips: tuple[str, ...] = ()


def network_cidr(network: str) -> str:
    """A network's IPv4 subnet, or raise."""
    proc = run_docker([
        "docker", "network", "inspect",
        "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", network,
    ])
    cidr = proc.stdout.strip()
    if proc.returncode != 0 or not cidr:
        raise ConsolidatedLaunchError(
            f"gateway network {network} has no subnet: {proc.stderr.strip()}"
        )
    return cidr


def network_container_ips(network: str) -> list[str]:
    """Every address assigned on a network, read from the network itself."""
    proc = run_docker([
        "docker", "network", "inspect", "--format",
        "{{range .Containers}}{{.IPv4Address}} {{end}}", network,
    ])
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise ConsolidatedLaunchError(
            f"could not inspect addresses on gateway network {network}: {detail}"
        )
    ips: list[str] = []
    for entry in proc.stdout.split():
        ips.append(entry.split("/", 1)[0])
    return ips


def _peer_ips(client: OrchestratorClient, own: str) -> tuple[str, ...]:
    """Every other registered bottle's source IP."""
    ips = {record.get("source_ip") for record in client.list_bottles()}
    return tuple(sorted(ip for ip in ips if isinstance(ip, str) and ip and ip != own))


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    service: DockerInfraService,
    agent_network: str,
    tokens: dict[str, str] | None = None,
    env_var_secret: str | None = None,
) -> LaunchContext:
    """Ensure the infra pair, create `agent_network` with the gateway on it,
    register the bottle and provision its git-gate state. Returns the attach
    context; on failure the network is removed again."""
    url = service.ensure_running(launch_stages=True)
    # Agents attribute against the gateway container, not the orchestrator.
    gateway = service.gateway()
    client = OrchestratorClient(url)

    subnet = create_agent_network(agent_network, service.settings.infra.docker_agent_subnets)
    try:
        gateway_ip, source_ip = agent_subnet_addresses(subnet)
        connect_gateway(agent_network, gateway.name, gateway_ip)
        peer_ips = _peer_ips(client, source_ip)
        transport = gateway.provisioning_transport()
        # A kept container keeps the ENV_VAR_SECRET it was made with.
        reg = provision_bottle(
            client, source_ip, egress_plan, git_gate_plan, transport,
            tokens=tokens, env_var_secret=env_var_secret,
        )
    except BaseException:
        remove_agent_network(agent_network, gateway.name)
        raise
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        network=agent_network,
        gateway_ip=gateway_ip,
        env_var_secret=reg.env_var_secret,
        peer_ips=peer_ips,
    )


def deprovision_consolidated(
    bottle_id: str, *, service: DockerInfraService, infra_name: str = INFRA_NAME,
    timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent."""
    deprovision_bottle(bottle_id, DockerGatewayTransport(infra_name),
                       orchestrator=service.orchestrator(),
                       timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "deprovision_consolidated",
    "ConsolidatedLaunchError",
]
