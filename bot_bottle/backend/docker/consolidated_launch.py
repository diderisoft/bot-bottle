"""Consolidated bottle launch sequence for the docker backend (PRD 0070).

Composes the orchestrator primitives into the register/teardown sequence:

  1. ensure the single infra container (control plane + gateway) is up;
  2. allocate the bottle a pinned source IP on the gateway network;
  3. register it and provision its git-gate repos/creds into the gateway.

Returns a `LaunchContext` with everything the agent container needs to
attach. The agent `docker run` itself is the backend's job; this owns the
orchestrator-facing wiring so that sequence stays testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...docker_cmd import run_docker
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...orchestrator.gateway import GATEWAY_NETWORK
from ...orchestrator.lifecycle import INFRA_NAME, OrchestratorService
from ..consolidated_util import provision_bottle
from ..consolidated_util import teardown_consolidated as _teardown_util
from .gateway_provision import DockerGatewayTransport
from .gateway_net import next_free_ip


class ConsolidatedLaunchError(RuntimeError):
    """The consolidated register/provision sequence could not complete."""


@dataclass(frozen=True)
class LaunchContext:
    """What the agent container needs to join the shared gateway."""

    bottle_id: str
    identity_token: str
    source_ip: str          # the agent's pinned address (attribution key)
    network: str            # the shared gateway network to attach to
    gateway_ip: str         # the gateway's address — the agent's proxy target
    orchestrator_url: str


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


def _container_ip(name: str, network: str) -> str:
    """A container's IPv4 address on `network`, or raise."""
    proc = run_docker([
        "docker", "inspect", "--format",
        f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}', name,
    ])
    ip = proc.stdout.strip()
    if proc.returncode != 0 or not ip:
        raise ConsolidatedLaunchError(
            f"container {name} has no address on {network}: {proc.stderr.strip()}"
        )
    return ip


def _network_container_ips(network: str) -> list[str]:
    """Every address currently assigned on the gateway network — the ground
    truth for "in use": the infra container and every live agent. Read from
    the network so a new bottle can't collide with anything actually attached."""
    proc = run_docker([
        "docker", "network", "inspect", "--format",
        "{{range .Containers}}{{.IPv4Address}} {{end}}", network,
    ])
    ips: list[str] = []
    for entry in proc.stdout.split():
        ips.append(entry.split("/", 1)[0])
    return ips


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
    service: OrchestratorService | None = None,
    infra_name: str = INFRA_NAME,
    network: str = GATEWAY_NETWORK,
) -> LaunchContext:
    """Ensure the infra container is up, allocate + register the bottle, and
    provision its git-gate state. Returns the agent's attach context."""
    service = service or OrchestratorService()
    url = service.ensure_running()
    client = OrchestratorClient(url)

    cidr = _network_cidr(network)
    gateway_ip = _container_ip(infra_name, network)
    source_ip = next_free_ip(cidr, _network_container_ips(network))

    transport = DockerGatewayTransport(infra_name)
    reg = provision_bottle(
        client, source_ip, egress_plan, git_gate_plan, transport,
        image_ref=image_ref, tokens=tokens,
    )
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        network=network,
        gateway_ip=gateway_ip,
        orchestrator_url=url,
    )


def teardown_consolidated(
    bottle_id: str, *, orchestrator_url: str, infra_name: str = INFRA_NAME,
    timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent."""
    _teardown_util(bottle_id, DockerGatewayTransport(infra_name),
                   orchestrator_url=orchestrator_url, timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "teardown_consolidated",
    "ConsolidatedLaunchError",
]
