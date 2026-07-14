"""Consolidated registration inputs (PRD 0070, docker slice).

Bridges the existing per-bottle `prepare` output to the consolidated
registry: turns a prepared bottle's egress plan into the backend-neutral
inputs `Orchestrator.launch_bottle` takes — the egress **policy** blob and
launch **metadata**.

The policy blob is the exact routes YAML the per-bottle egress sidecar used
to read from a file; in the consolidated model the multi-tenant gateway's
`PolicyResolver` fetches it from the registry per request (keyed by source
IP) instead. Same render, so consolidated and single-tenant egress apply
byte-identical policy — a bottle's allow-list doesn't change when it moves
onto the shared gateway.

Host-side glue (imports `bot_bottle.egress`), used by the launch path — not
by the lean orchestrator control-plane process itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..egress import EgressPlan, egress_render_routes


@dataclass(frozen=True)
class RegistrationInputs:
    """What `Orchestrator.launch_bottle` needs to register a bottle, derived
    from its prepared plan. `policy` is served verbatim by the gateway's
    `/resolve`; `metadata` is opaque forward-compat state — it carries the
    human slug so the console / supervise can show a name, not just the
    minted bottle id."""

    policy: str
    metadata: str


def egress_policy(plan: EgressPlan) -> str:
    """The bottle's egress policy blob: the routes YAML the gateway serves
    and the addon parses with `load_config`. Identical to the per-bottle
    `routes.yaml` render, so the consolidated path applies the same
    allow-list."""
    return egress_render_routes(plan.routes, log=plan.log)


def registration_inputs(plan: EgressPlan) -> RegistrationInputs:
    """Assemble the orchestrator registration inputs from a prepared egress
    plan. `metadata` records the slug so the shared registry can map a minted
    bottle id back to its human name."""
    return RegistrationInputs(
        policy=egress_policy(plan),
        metadata=json.dumps({"slug": plan.slug}),
    )


__all__ = ["RegistrationInputs", "egress_policy", "registration_inputs"]
