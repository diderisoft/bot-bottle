"""Consolidated bottle launch sequence for the Firecracker backend
(PRD 0070, Stage B).

The shared gateway + orchestrator control plane run in a single persistent
per-host **infra VM** (`infra_vm.py`), not Docker containers. Agent VMs reach
the gateway's egress / supervise / git-http ports at the infra VM via a
PREROUTING DNAT on their own host-side TAP IP (see
`scripts/firecracker-netpool.sh`), and the host CLI reaches the control plane
over HTTP at the infra VM's guest IP.

Attribution is by the agent VM's guest IP, unspoofable by construction: the
/31 point-to-point TAP + the `bot_bottle_fc` nft table ensure only the
expected VM can source-IP that address.

Sequence:
  1. ensure the infra VM (control plane + gateway) is up (a singleton — a
     prior launcher may already have booted it);
  2. register the bottle by its guest IP (attribution key) → bottle id +
     identity token;
  3. provision its git-gate repos/creds into the gateway VM (over SSH);
  4. fetch the shared gateway CA for the provisioner to install in the rootfs.

The TAP slot allocation, rootfs build, and VM boot are the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...orchestrator.lifecycle import (
    OrchestratorStartError,  # re-exported so callers can catch it
)
from ..consolidated_util import provision_bottle, teardown_consolidated as _teardown_util
from . import infra_vm


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
    env_var_secret: str = ""  # encryption key injected into the agent's env


def launch_consolidated(
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    *,
    guest_ip: str,
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
) -> LaunchContext:
    """Ensure the infra VM is up, register the bottle by its guest IP, and
    provision its git-gate state into the gateway VM. Returns the context the
    agent-VM launch needs. Raises on failure — the caller tears down."""
    infra = infra_vm.ensure_running()
    url = infra.control_plane_url
    client = OrchestratorClient(url)

    transport = infra_vm.gateway_transport()
    reg = provision_bottle(
        client, guest_ip, egress_plan, git_gate_plan, transport,
        image_ref=image_ref, tokens=tokens,
    )
    # The shared gateway CA every agent on this host trusts for TLS
    # interception — fetched from the infra VM over SSH.
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=guest_ip,
        gateway_ca_pem=infra.gateway_ca_pem(),
        orchestrator_url=url,
        env_var_secret=reg.env_var_secret,
    )


def teardown_consolidated(
    bottle_id: str, *, orchestrator_url: str, timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state from the gateway
    VM. Both steps are idempotent so this is safe from a cleanup trap. Does
    NOT stop the infra VM — it's a persistent per-host singleton shared by
    every bottle."""
    _teardown_util(bottle_id, infra_vm.gateway_transport(),
                   orchestrator_url=orchestrator_url, timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "teardown_consolidated",
    "ConsolidatedLaunchError",
    "OrchestratorStartError",
]
