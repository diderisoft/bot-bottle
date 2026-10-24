"""Docker register/teardown sequence: ensure the infra pair, allocate a pinned
source IP on the gateway network, register the bottle and provision its
git-gate state. Returns the agent's `LaunchContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import run_docker
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...control.client import OrchestratorClient
from ...control.gateway import GATEWAY_NETWORK
from .infra import INFRA_NAME, DockerInfraService
from ..provision_bottle import deprovision_bottle, provision_bottle
from .gateway_transport import DockerGatewayTransport
from .gateway_net import next_free_ip


class ConsolidatedLaunchError(RuntimeError):
    """The register/provision sequence couldn't complete."""


@dataclass(frozen=True)
class LaunchContext:
    """What the agent container needs to join the shared gateway."""

    bottle_id: str
    identity_token: str
    source_ip: str  # pinned address (attribution key)
    network: str  # shared gateway network
    gateway_ip: str  # proxy target
    env_var_secret: str = ""  # key for the agent's env


def _network_cidr(network: str) -> str:
    """The gateway network's IPv4 subnet, or raise."""
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


def _network_container_ips(network: str) -> list[str]:
    """Every address assigned on the gateway network, read from the network
    itself."""
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


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    service: DockerInfraService,
    tokens: dict[str, str] | None = None,
    infra_name: str = INFRA_NAME,
    network: str = GATEWAY_NETWORK,
) -> LaunchContext:
    """Ensure the infra pair, allocate and register the bottle, and provision
    its git-gate state. Returns the attach context."""
    url = service.ensure_running(launch_stages=True)
    # Agents attribute against the gateway container, not the orchestrator.
    gateway = service.gateway()
    client = OrchestratorClient(url)

    cidr = _network_cidr(network)
    gateway_ip = gateway.address()
    source_ip = next_free_ip(cidr, [*_network_container_ips(network), gateway_ip])

    transport = gateway.provisioning_transport()
    reg = provision_bottle(
        client, source_ip, egress_plan, git_gate_plan, transport,
        tokens=tokens,
    )
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        network=network,
        gateway_ip=gateway_ip,
        env_var_secret=reg.env_var_secret,
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
