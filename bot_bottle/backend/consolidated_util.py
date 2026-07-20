"""Shared helpers for the consolidated launch sequence (PRD 0070).

Logic that was duplicated across the docker, macos_container, and
firecracker consolidated_launch modules — extracted so each backend
imports it rather than re-implementing it.
"""

from __future__ import annotations

from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..orchestrator.client import OrchestratorClient
from ..orchestrator.registration import registration_inputs
from .docker.gateway_provision import GatewayTransport, deprovision_git_gate, provision_git_gate


def provision_bottle(
    client: OrchestratorClient,
    source_ip: str,
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    transport: GatewayTransport,
    *,
    image_ref: str = "",
    tokens: dict[str, str] | None = None,
):
    """Register the bottle and provision its git-gate state. Rolls back the
    registration if provisioning fails so no orphan is left. Returns the
    `RegisteredBottle` from the orchestrator."""
    inputs = registration_inputs(egress_plan)
    reg = client.register_bottle(
        source_ip, image_ref=image_ref, policy=inputs.policy,
        metadata=inputs.metadata, tokens=tokens,
    )
    try:
        provision_git_gate(transport, reg.bottle_id, git_gate_plan)
    except Exception:
        client.teardown_bottle(reg.bottle_id)
        raise
    return reg


def teardown_consolidated(
    bottle_id: str,
    transport: GatewayTransport,
    *,
    orchestrator_url: str,
    timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Both steps are
    idempotent so this is safe from a cleanup trap."""
    from ..orchestrator.config_store import DEFAULT_TEARDOWN_TIMEOUT_SECONDS
    OrchestratorClient(
        orchestrator_url,
        timeout=timeout if timeout is not None else DEFAULT_TEARDOWN_TIMEOUT_SECONDS,
    ).teardown_bottle(bottle_id)
    deprovision_git_gate(transport, bottle_id)


__all__ = ["provision_bottle", "teardown_consolidated"]
