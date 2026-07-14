"""Consolidated bottle launch sequence for the Firecracker backend (PRD 0070).

Mirrors bot_bottle.backend.docker.consolidated_launch but wired for
Firecracker's TAP-based network topology instead of a shared Docker bridge.

The per-bottle sidecar bundle (one `docker run` per bottle, published on the
slot's host-side TAP IP) is replaced by a single persistent gateway that
every Firecracker VM shares. The gateway runs as a Docker container in the
dev-harness (a Firecracker VM is stage B per PRD 0070), with its ports
published on the host (`0.0.0.0:PORT`). VMs reach it at their slot's
host-side TAP IP because Docker's iptables PREROUTING DNAT redirects
port 9099/9100/9420 traffic to the gateway container — a path the nft
isolation table already allows via `ct status dnat accept` in the forward
chain.

Attribution is by the VM's guest IP, which is unspoofable by construction:
the /31 point-to-point TAP topology + the `bot_bottle_fc` nft table ensure
that only the expected VM can source-IP that address.

Sequence:
  1. ensure the Firecracker-flavoured orchestrator + gateway are up;
  2. register the bottle by its guest IP (attribution key) → bottle id +
     identity token;
  3. provision its git-gate repos/creds into the running gateway;
  4. fetch the shared gateway CA for the provisioner to install in the rootfs.

The TAP slot allocation, rootfs build, and VM boot are the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...orchestrator.gateway import (
    GATEWAY_NETWORK,
    DockerGateway,
)
from ...orchestrator.lifecycle import (
    OrchestratorService,
    OrchestratorStartError,  # re-exported so callers can catch it
)
from ...orchestrator.registration import registration_inputs
from ..docker.egress import EGRESS_PORT
from ..docker.gateway_provision import deprovision_git_gate, provision_git_gate
from ...supervise import SUPERVISE_PORT

_GIT_HTTP_PORT = 9420

# Separate names from the Docker gateway so both backends can coexist on one
# host (and for clarity in `docker ps` output).
_FC_GATEWAY_NAME = "bot-bottle-fc-gateway"
_FC_ORCHESTRATOR_NAME = "bot-bottle-fc-orchestrator"
_FC_ORCHESTRATOR_LABEL = "bot-bottle-fc-orchestrator=1"
# Ports the gateway publishes on the host so Firecracker VMs can reach it
# via their TAP link. Docker's PREROUTING DNAT + nft's `ct status dnat
# accept` in the forward chain route the traffic.
_FC_GATEWAY_HOST_PORTS = (EGRESS_PORT, SUPERVISE_PORT, _GIT_HTTP_PORT)


class ConsolidatedLaunchError(RuntimeError):
    """The consolidated register/provision sequence could not complete."""


@dataclass(frozen=True)
class LaunchContext:
    """What the Firecracker launch needs from the consolidated sequence."""

    bottle_id: str
    identity_token: str
    source_ip: str          # the VM's guest IP — the attribution key
    gateway_ca_pem: str     # the shared gateway CA the provisioner installs
    orchestrator_url: str


class _FirecrackerOrchestratorService(OrchestratorService):
    """Dev-harness orchestrator for the Firecracker backend.

    Uses a gateway that publishes its ports on the host so Firecracker VMs can
    reach it via their TAP link. The gateway and orchestrator containers use
    `*-fc-*` names so both backends can run independently on the same host.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            orchestrator_name=_FC_ORCHESTRATOR_NAME,
            orchestrator_label=_FC_ORCHESTRATOR_LABEL,
            **kwargs,  # type: ignore[arg-type]
        )

    def _gateway(self) -> DockerGateway:
        # The heavy data-plane image (#384 split it from the lean control-plane
        # `image` this service's orchestrator container runs).
        return DockerGateway(
            self._gateway_image,
            name=_FC_GATEWAY_NAME,
            network=self.network,
            orchestrator_url=self.internal_url,
            host_port_bindings=_FC_GATEWAY_HOST_PORTS,
        )


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    guest_ip: str,
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
    service: OrchestratorService | None = None,
    gateway_name: str = _FC_GATEWAY_NAME,
) -> LaunchContext:
    """Ensure the orchestrator + Firecracker gateway are up, register the
    bottle by its guest IP, and provision its git-gate state. Returns the
    context the VM launch needs. Raises `ConsolidatedLaunchError` (or the
    primitives' own errors) on failure — the caller tears down on failure."""
    service = service or _FirecrackerOrchestratorService()
    url = service.ensure_running()
    client = OrchestratorClient(url)

    inputs = registration_inputs(egress_plan)
    reg = client.register_bottle(
        guest_ip, image_ref=image_ref, policy=inputs.policy,
        metadata=inputs.metadata, tokens=tokens,
    )
    try:
        provision_git_gate(gateway_name, reg.bottle_id, git_gate_plan)
    except Exception:
        client.teardown_bottle(reg.bottle_id)
        raise

    # Fetch the shared gateway CA here so the caller can install it in the
    # rootfs (the same CA every agent on this host trusts for TLS interception).
    gateway_ca_pem = DockerGateway(
        name=gateway_name, network=GATEWAY_NETWORK,
    ).ca_cert_pem()

    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=guest_ip,
        gateway_ca_pem=gateway_ca_pem,
        orchestrator_url=url,
    )


def teardown_consolidated(
    bottle_id: str, *, orchestrator_url: str, gateway_name: str = _FC_GATEWAY_NAME,
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
    "OrchestratorStartError",
]
