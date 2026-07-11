"""Prepare step for the Firecracker backend."""

from __future__ import annotations

from pathlib import Path

from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...manifest import Manifest
from ...supervise import SupervisePlan
from .. import BottleSpec
from . import util
from .bottle_plan import FirecrackerBottlePlan


def preflight() -> None:
    util.require_firecracker()


def build_guest_env(resolved_env: ResolvedEnv) -> dict[str, str]:
    return dict(resolved_env.literals)


def resolve_plan(
    spec: BottleSpec,
    manifest: Manifest,
    slug: str,
    resolved_env: ResolvedEnv,
    agent_provision_plan: AgentProvisionPlan,
    egress_plan: EgressPlan,
    supervise_plan: SupervisePlan | None,
    git_gate_plan: GitGatePlan,
    stage_dir: Path,
) -> FirecrackerBottlePlan:
    return FirecrackerBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=stage_dir,
        slug=slug,
        forwarded_env=dict(resolved_env.forwarded),
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        agent_provision=agent_provision_plan,
    )
