"""Registration inputs: a prepared bottle's egress plan as the policy blob and
metadata `OrchestratorCore.register_bottle` takes. The gateway's
`PolicyResolver` fetches the policy per request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..egress import EgressPlan, egress_render_routes


@dataclass(frozen=True)
class RegistrationInputs:
    """What registration needs. `policy` is served verbatim by `/resolve`;
    `metadata` carries the human slug for display."""

    policy: str
    metadata: str


def egress_policy(plan: EgressPlan) -> str:
    """The policy blob: the routes YAML the addon parses with `load_config`."""
    return egress_render_routes(plan.routes, log=plan.log,
                                audit_delivery=plan.audit_delivery, firehose=plan.firehose)


def registration_inputs(plan: EgressPlan) -> RegistrationInputs:
    """Registration inputs from a prepared egress plan."""
    return RegistrationInputs(
        policy=egress_policy(plan),
        metadata=json.dumps({"slug": plan.slug, "audit_delivery": plan.audit_delivery}),
    )


__all__ = ["RegistrationInputs", "egress_policy", "registration_inputs"]
