"""Register a bottle with the orchestrator and provision its git-gate state
into the gateway, and the inverse. Backends drive these through their
`GatewayTransport`.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..control.client import OrchestratorClient, OrchestratorClientError, RegisteredBottle
from ..control.registration import registration_inputs
from ..orchestrator.store.secret_store import new_env_var_secret
from .provision_gateway import GatewayTransport, deprovision_git_gate, provision_git_gate

if TYPE_CHECKING:
    from ..control.lifecycle import Orchestrator


def provision_bottle(
    client: OrchestratorClient,
    source_ip: str,
    egress_plan: EgressPlan,
    git_gate_plan: GitGatePlan,
    transport: GatewayTransport,
    *,
    tokens: dict[str, str] | None = None,
    env_var_secret: str | None = None,
) -> RegisteredBottle:
    """Register the bottle and provision its git-gate state, rolling the
    registration back if provisioning fails. A fresh ENV_VAR_SECRET goes to the
    orchestrator (to encrypt tokens at rest) and onto the returned
    `RegisteredBottle` for the agent's env."""
    inputs = registration_inputs(egress_plan)
    env_var_secret = env_var_secret or new_env_var_secret()
    reg = client.register_bottle(
        source_ip, policy=inputs.policy,
        metadata=inputs.metadata, tokens=tokens, env_var_secret=env_var_secret,
    )
    try:
        provision_git_gate(transport, reg.bottle_id, git_gate_plan)
    except Exception:
        client.teardown_bottle(reg.bottle_id)
        raise
    return dataclasses.replace(reg, env_var_secret=env_var_secret)


def deprovision_bottle(
    bottle_id: str,
    transport: GatewayTransport,
    *,
    orchestrator: Orchestrator,
    timeout: float | None = None,
) -> None:
    """Deregister the bottle and remove its git-gate state. Idempotent. The
    orchestrator is looked up now, since it may have moved while the bottle
    ran."""
    from ..orchestrator.teardown_timeout import DEFAULT_TEARDOWN_TIMEOUT_SECONDS
    url = orchestrator.url()
    if not url:
        raise OrchestratorClientError(
            f"DELETE /bottles/{bottle_id}: the orchestrator isn't running")
    OrchestratorClient(
        url, timeout=timeout if timeout is not None else DEFAULT_TEARDOWN_TIMEOUT_SECONDS,
    ).teardown_bottle(bottle_id)
    deprovision_git_gate(transport, bottle_id)


__all__ = ["provision_bottle", "deprovision_bottle"]
