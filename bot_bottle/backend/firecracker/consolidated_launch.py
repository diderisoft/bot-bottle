"""Firecracker register/teardown sequence.

The infra pair runs in two persistent VMs (`infra_vm.py`). Agent VMs reach the
gateway ports through a DNAT on their TAP's host IP to the gateway VM, never
the orchestrator. Attribution is by guest IP, which the /31 TAP and the
`bot_bottle_fc` table make unspoofable.

Ensure the infra VMs, register the bottle by guest IP, provision its git-gate
state over SSH, and fetch the gateway CA. TAP allocation, rootfs and boot are
the caller's.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...egress import EgressPlan
from ...git_gate import GitGatePlan
from ...control.client import OrchestratorClient
from ...control.lifecycle import (
    OrchestratorStartError,  # re-exported for callers
)
from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
from ..provision_bottle import deprovision_bottle, provision_bottle
from . import util
from .infra import FirecrackerInfraService


class ConsolidatedLaunchError(RuntimeError):
    """The register/provision sequence couldn't complete."""


@dataclass(frozen=True)
class LaunchContext:
    """What the VM launch needs from the register sequence."""

    bottle_id: str
    identity_token: str
    source_ip: str  # guest IP (attribution key)
    gateway_ca_pem: str  # the gateway CA to install
    env_var_secret: str = ""  # key for the agent's env


def persist_env_var_secret(private_key: Path, guest_ip: str, secret: str) -> None:
    """Copy the key into guest tmpfs so a gateway restart can recover it."""
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
    service: FirecrackerInfraService,
    guest_ip: str,
    tokens: dict[str, str] | None = None,
) -> LaunchContext:
    """Ensure the infra VMs, register the bottle by guest IP and provision its
    git-gate state. Raises on failure; the caller tears down."""
    url = service.ensure_running(launch_stages=True)
    client = OrchestratorClient(url)

    gateway = service.gateway()
    transport = gateway.provisioning_transport()
    reg = provision_bottle(
        client, guest_ip, egress_plan, git_gate_plan, transport,
        tokens=tokens,
    )
    # The gateway CA, fetched from the gateway VM over SSH.
    return LaunchContext(
        bottle_id=reg.bottle_id,
        identity_token=reg.identity_token,
        source_ip=guest_ip,
        gateway_ca_pem=gateway.ca_cert_pem(),
        env_var_secret=reg.env_var_secret,
    )


def deprovision_consolidated(
    bottle_id: str, *, service: FirecrackerInfraService, timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent. The
    infra VMs keep running."""
    deprovision_bottle(bottle_id, service.gateway().provisioning_transport(),
                       orchestrator=service.orchestrator(),
                       timeout=timeout)


__all__ = [
    "LaunchContext",
    "launch_consolidated",
    "deprovision_consolidated",
    "ConsolidatedLaunchError",
    "OrchestratorStartError",
]
