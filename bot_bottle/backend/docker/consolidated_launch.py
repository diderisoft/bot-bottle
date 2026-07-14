"""Consolidated bottle launch sequence for the docker backend (PRD 0070).

Composes the orchestrator primitives into the register/teardown sequence that
replaces the per-bottle sidecar bundle:

  1. ensure the orchestrator control plane + shared gateway are up;
  2. allocate the bottle a pinned source IP on the gateway network (the
     attribution key), skipping the gateway's own address + live bottles;
  3. register it (egress policy blob + slug metadata) → bottle id + identity
     token;
  4. provision its git-gate repos/creds into the running gateway.

It returns a `LaunchContext` with everything the agent container needs to
attach — network, pinned IP, the gateway's address (its proxy target), the
orchestrator URL, and the identity token. The agent `docker run` itself is
the backend's job (it owns provider provisioning); this owns the
orchestrator-facing wiring so that sequence stays testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...docker_cmd import run_docker
from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...orchestrator.gateway import GATEWAY_NAME, GATEWAY_NETWORK
from ...orchestrator.lifecycle import OrchestratorService
from ...orchestrator.registration import registration_inputs
from .gateway_net import next_free_ip
from .gateway_provision import deprovision_git_gate, provision_git_gate


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
            f"gateway {name} has no address on {network}: {proc.stderr.strip()}"
        )
    return ip


def _network_container_ips(network: str) -> list[str]:
    """Every address currently assigned on the gateway network — the ground
    truth for "in use": the gateway + orchestrator infrastructure containers
    and every live agent. Read from the network so a new bottle can't collide
    with anything actually attached (a registry-only view would miss the
    orchestrator/gateway containers)."""
    proc = run_docker([
        "docker", "network", "inspect", "--format",
        "{{range .Containers}}{{.IPv4Address}} {{end}}", network,
    ])
    ips: list[str] = []
    for entry in proc.stdout.split():
        # entries look like "172.20.0.2/16" — keep the address.
        ips.append(entry.split("/", 1)[0])
    return ips


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    image_ref: str = "",
    service: OrchestratorService | None = None,
    gateway_name: str = GATEWAY_NAME,
    network: str = GATEWAY_NETWORK,
) -> LaunchContext:
    """Ensure the orchestrator + gateway are up, allocate + register the
    bottle, and provision its git-gate state. Returns the agent's attach
    context. Raises `ConsolidatedLaunchError` (or the primitives' own errors)
    if any step fails — the caller tears down on failure."""
    service = service or OrchestratorService()
    url = service.ensure_running()
    client = OrchestratorClient(url)

    cidr = _network_cidr(network)
    gateway_ip = _container_ip(gateway_name, network)
    source_ip = next_free_ip(cidr, _network_container_ips(network))

    inputs = registration_inputs(egress_plan)
    reg = client.register_bottle(
        source_ip, image_ref=image_ref, policy=inputs.policy, metadata=inputs.metadata,
    )
    try:
        provision_git_gate(gateway_name, reg.bottle_id, git_gate_plan)
    except Exception:
        # Roll the registration back so a provisioning failure leaves no orphan.
        client.teardown_bottle(reg.bottle_id)
        raise
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=source_ip,
        network=network,
        gateway_ip=gateway_ip,
        orchestrator_url=url,
    )


def teardown_consolidated(
    bottle_id: str, *, orchestrator_url: str, gateway_name: str = GATEWAY_NAME,
) -> None:
    """Deregister the bottle and remove its git-gate state from the gateway.
    Both steps are idempotent so this is safe from a cleanup trap."""
    OrchestratorClient(orchestrator_url).teardown_bottle(bottle_id)
    deprovision_git_gate(gateway_name, bottle_id)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "teardown_consolidated",
    "ConsolidatedLaunchError",
]
