"""Consolidated bottle launch sequence for the docker backend (PRD 0070).

Composes the orchestrator primitives into the register/teardown sequence:

  1. ensure the per-host pair (orchestrator + gateway containers) is up;
  2. allocate the bottle a pinned source IP on the gateway network;
  3. register it and provision its git-gate repos/creds into the gateway.

Returns a `LaunchContext` with everything the agent container needs to
attach. The agent `docker run` itself is the backend's job; this owns the
orchestrator-facing wiring so that sequence stays testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import run_docker
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...gateway import GATEWAY_NETWORK
from .infra import INFRA_NAME, DockerInfraService
from ..provision_bottle import deprovision_bottle, provision_bottle
from .gateway_transport import DockerGatewayTransport
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
    env_var_secret: str = ""  # encryption key injected into the agent's env


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
    """Every address currently assigned on the gateway network — the ground
    truth for "in use": the gateway container and every live agent. Read from
    the network so a new bottle can't collide with anything actually attached."""
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
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
    service: DockerInfraService | None = None,
    infra_name: str = INFRA_NAME,
    network: str = GATEWAY_NETWORK,
) -> LaunchContext:
    """Ensure the orchestrator + gateway pair is up, allocate + register the bottle, and
    provision its git-gate state. Returns the agent's attach context. (Bringing
    the pair up cold also reattaches already-running bottles; see
    `InfraService.reattach_running_bottles`.)"""
    service = service or DockerInfraService()
    url = service.ensure_running()
    # Agents attribute against the *gateway* container (data plane), not the
    # orchestrator — the two planes are now separate containers. Read its
    # agent-facing address + provisioning transport off the Gateway service.
    gateway = service.gateway()
    client = OrchestratorClient(url)

    cidr = _network_cidr(network)
    gateway_ip = gateway.address()
    source_ip = next_free_ip(cidr, [*_network_container_ips(network), gateway_ip])

    transport = gateway.provisioning_transport()
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
        env_var_secret=reg.env_var_secret,
    )


def deprovision_consolidated(
    bottle_id: str, *, infra_name: str = INFRA_NAME, timeout: float | None = None,
    service: DockerInfraService | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent."""
    deprovision_bottle(bottle_id, DockerGatewayTransport(infra_name),
                       orchestrator=(service or DockerInfraService()).orchestrator(),
                       timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "deprovision_consolidated",
    "ConsolidatedLaunchError",
]
