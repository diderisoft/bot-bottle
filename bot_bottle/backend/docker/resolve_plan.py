"""Prepare step for the Docker bottle backend.

`resolve_plan` does all host-side resolution (image and container
names, prompt-file, proxy plan, runtime detection) and returns a
frozen DockerBottlePlan. No Docker resources are created; the only
side effects are scratch files under `stage_dir` and a probe of
`docker info`. Cross-backend host-side validation has already run
via the base class's `prepare` template before this is called.
"""

from __future__ import annotations

from pathlib import Path

from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from .. import BottleSpec
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
    settings: Settings,
    secrets: SecretSource,
) -> DockerBottlePlan:
    """Resolve Docker-specific names and write scratch files. Trusts
    that the agent and its skills/git-gate keys are present —
    validation already ran in the base class."""

    # ==== docker specific setup ====
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
