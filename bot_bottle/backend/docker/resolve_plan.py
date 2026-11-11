"""Docker `resolve_plan`: image and container names, prompt file, proxy plan,
runtime detection. Creates no Docker resources; writes scratch files under
`stage_dir` and probes `docker info`.
"""

from __future__ import annotations

from pathlib import Path

from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from .. import BottleSpec
from ..resolve_common import build_guest_env  # noqa: F401 — re-exported for callers
from ...contract.hostctl import Settings
from ...env import ResolvedEnv
from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...manifest import Manifest
from ...secret_source import SecretSource
from ...supervisor.plan import SupervisePlan
from ...git_gate import GitGatePlan

def preflight() -> None:
    docker_mod.require_docker()


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
    settings: Settings,
    secrets: SecretSource,
) -> DockerBottlePlan:
    """Resolve docker-specific names and write scratch files. Validation has
    already run."""

    use_runsc = docker_mod.runsc_available()

    return DockerBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=stage_dir,
        slug=slug,
        forwarded_env=dict(resolved_env.forwarded),
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        use_runsc=use_runsc,
        agent_provision=agent_provision_plan,
        settings=settings,
        secrets=secrets,
    )
