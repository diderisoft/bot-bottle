"""smolmachines `_resolve_plan` (PRD 0023 chunks 2d + 4c).

Resolves the per-bottle docker subnet + bundle IP and assembles
the guest env. The agent's docker image build → smolmachine
pack pipeline runs in `launch.launch`, not here, so the
dashboard's preflight modal isn't garbled by docker-build output
before the operator has confirmed.

No VM bringup — that's `launch.launch`'s job."""

from __future__ import annotations

from pathlib import Path

from .. import BottleSpec
from ...env import ResolvedEnv
from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...supervise import SupervisePlan
from ...git_gate import GitGatePlan

from ...backend import BottleSpec
from ...env import ResolvedEnv
# from ...workspace import workspace_plan as resolve_workspace_plan
from .bottle_plan import SmolmachinesBottlePlan
from .util import smolmachines_bundle_subnet, smolmachines_preflight

def preflight():
    smolmachines_preflight()

def build_guest_env(resolved_env: ResolvedEnv):
    # Agent's env: resolve through resolve_env() so ?prompt entries
    # are prompted and ${HOST_VAR} entries are interpolated — matching
    # the Docker backend's contract. Forwarded (secret/interpolated)
    # values still reach the guest as -e K=V smolvm flags because
    # smolvm 0.8.0 has no env-file or stdin injection path; this is
    # the known argv-exposure gap documented in PRD 0038.
    # HTTPS_PROXY / GIT_GATE_URL / MCP_SUPERVISE_URL are populated
    # in launch.py after bundle bringup.
    return {
        **resolved_env.literals,
        **resolved_env.forwarded,
        "NO_PROXY":    "localhost,127.0.0.1",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
        "SSL_CERT_FILE":       "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE":  "/etc/ssl/certs/ca-certificates.crt",
    }


def resolve_plan(
    spec: BottleSpec,
    slug: str,
    resolved_env: ResolvedEnv,
    instance_name: str,
    agent_image: str,
    agent_dockerfile_path: str,
    prompt_file: Path,
    agent_provision_plan: AgentProvisionPlan,
    egress_plan: EgressPlan,
    supervise_plan: SupervisePlan,
    git_gate_plan: GitGatePlan,
    stage_dir: Path,
) -> SmolmachinesBottlePlan:
    """Materialize the smolmachines plan. The bundle's docker
    subnet + pinned IP are derived from the slug; the agent's
    `.smolmachine` artifact is built (or cache-hit) here so
    launch's `machine create --from` boots without a registry
    pull. Per-bottle guest env + the TSI allow_cidrs land on the
    plan for launch to pass straight through to
    `machine create` flags."""

    # ==== smolmachines specific setup ====
    subnet, gateway, bundle_ip = smolmachines_bundle_subnet(slug)

    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        bundle_subnet=subnet,
        bundle_gateway=gateway,
        bundle_ip=bundle_ip,
        machine_name=instance_name,
        agent_image=agent_image,
        guest_env=agent_provision_plan.guest_env,
        prompt_file=prompt_file,
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        agent_provision=agent_provision_plan,
        # workspace_plan=workspace_plan,
    )
