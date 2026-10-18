"""Consolidated bottle launch sequence for the Firecracker backend
(PRD 0070, Stage B).

The orchestrator control plane and the shared gateway run in **two** persistent
per-host **infra VMs** (`infra_vm.py`), split per PRD 0070 — not Docker
containers. Agent VMs reach the gateway's egress / supervise / git-http ports
via a PREROUTING DNAT on their own host-side TAP IP that redirects to the
**gateway** VM (see `scripts/firecracker-netpool.sh`) — never the orchestrator,
so a breached agent has no L3 route to the control plane. The host CLI reaches
the control plane over HTTP at the orchestrator VM's guest IP.

Attribution is by the agent VM's guest IP, unspoofable by construction: the
/31 point-to-point TAP + the `bot_bottle_fc` nft table ensure only the
expected VM can source-IP that address.

Sequence:
  1. ensure the infra VMs (orchestrator control plane + gateway data plane)
     are up (a singleton pair — a prior launcher may already have booted them);
  2. register the bottle on the orchestrator by its guest IP (attribution key)
     → bottle id + identity token;
  3. provision its git-gate repos/creds into the gateway VM (over SSH);
  4. fetch the shared gateway CA for the provisioner to install in the rootfs.

The TAP slot allocation, rootfs build, and VM boot are the caller's job.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...orchestrator.client import OrchestratorClient
from ...orchestrator.lifecycle import (
    OrchestratorStartError,  # re-exported so callers can catch it
)
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..provision_bottle import deprovision_bottle, provision_bottle
from . import util
from .gateway import FirecrackerGateway
from .infra import FirecrackerInfraService


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


def persist_env_var_secret(private_key: Path, guest_ip: str, secret: str) -> None:
    """Mirror the exec-time key into guest tmpfs for restart recovery."""
    proc = subprocess.run(
        util.ssh_base_argv(private_key, guest_ip)
        + [f"umask 077; mkdir -p /run/bot-bottle; cat > {util.ENV_VAR_SECRET_PATH}"],
        input=secret, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise ConsolidatedLaunchError(
            f"failed to persist {ENV_VAR_SECRET_NAME} in agent VM: "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )


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
    service = FirecrackerInfraService()
    url = service.ensure_running()
    client = OrchestratorClient(url)

    # Read the gateway's provisioning transport + CA off the Gateway service.
    gateway = service.gateway()
    transport = gateway.provisioning_transport()
    reg = provision_bottle(
        client, guest_ip, egress_plan, git_gate_plan, transport,
        image_ref=image_ref, tokens=tokens,
    )
    # The shared gateway CA every agent on this host trusts for TLS
    # interception — fetched from the gateway VM over SSH.
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=guest_ip,
        gateway_ca_pem=gateway.ca_cert_pem(),
        orchestrator_url=url,
        env_var_secret=reg.env_var_secret,
    )


def deprovision_consolidated(
    bottle_id: str, *, orchestrator_url: str, timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state from the gateway
    VM. Both steps are idempotent so this is safe from a cleanup trap. Does
    NOT stop the infra VMs — they're persistent per-host singletons shared by
    every bottle."""
    deprovision_bottle(bottle_id, FirecrackerGateway().provisioning_transport(),
                   orchestrator_url=orchestrator_url, timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "deprovision_consolidated",
    "ConsolidatedLaunchError",
    "OrchestratorStartError",
]
