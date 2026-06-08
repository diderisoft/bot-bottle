"""smolmachines `_resolve_plan` (PRD 0023 chunks 2d + 4c).

Resolves the per-bottle docker subnet + bundle IP and assembles
the guest env. The agent's docker image build → smolmachine
pack pipeline runs in `launch.launch`, not here, so the
dashboard's preflight modal isn't garbled by docker-build output
before the operator has confirmed.

No VM bringup — that's `launch.launch`'s job."""

from __future__ import annotations

import os
from pathlib import Path

from ...agent_provider import PROVIDER_TEMPLATES, agent_provision_plan, get_provider
from ...backend import BottleSpec
from ...env import resolve_env
from ...workspace import workspace_plan as resolve_workspace_plan
from ..resolve_common import (
    merge_provision_env_vars,
    mint_slug,
    prepare_agent_state_dir,
    prepare_egress,
    prepare_git_gate,
    prepare_supervise,
    resolve_manifest_dockerfile,
    write_launch_metadata,
)
from .bottle_plan import SmolmachinesBottlePlan
from .util import smolmachines_bundle_subnet, smolmachines_preflight


# Gateway ports the bundle exposes inside its container — git-gate's
# git-daemon, supervise's MCP. The agent inside the smolvm guest
# dials these on the bundle's pinned IP.
_BUNDLE_GIT_GATE_PORT = 9418
_BUNDLE_SUPERVISE_PORT = 9100


def resolve_plan(
    spec: BottleSpec, *, stage_dir: Path
) -> SmolmachinesBottlePlan:
    """Materialize the smolmachines plan. The bundle's docker
    subnet + pinned IP are derived from the slug; the agent's
    `.smolmachine` artifact is built (or cache-hit) here so
    launch's `machine create --from` boots without a registry
    pull. Per-bottle guest env + the TSI allow_cidrs land on the
    plan for launch to pass straight through to
    `machine create` flags."""
    smolmachines_preflight()

    manifest = spec.manifest
    bottle = manifest.bottle_for(spec.agent_name)
    provider = bottle.agent_provider
    provider_obj = get_provider(provider.template)
    provider_runtime = provider_obj.runtime
    guest_home = "/home/node"
    workspace_plan = resolve_workspace_plan(spec, guest_home=guest_home)

    slug = mint_slug(spec)
    write_launch_metadata(slug, spec, compose_project="", backend="smolmachines")

    subnet, gateway, bundle_ip = smolmachines_bundle_subnet(slug)

    # Agent's env: resolve through resolve_env() so ?prompt entries
    # are prompted and ${HOST_VAR} entries are interpolated — matching
    # the Docker backend's contract. Forwarded (secret/interpolated)
    # values still reach the guest as -e K=V smolvm flags because
    # smolvm 0.8.0 has no env-file or stdin injection path; this is
    # the known argv-exposure gap documented in PRD 0038.
    # HTTPS_PROXY / GIT_GATE_URL / MCP_SUPERVISE_URL are populated
    # in launch.py after bundle bringup.
    resolved = resolve_env(manifest, spec.agent_name)
    guest_env: dict[str, str] = {
        **resolved.literals,
        **resolved.forwarded,
        "NO_PROXY":    "localhost,127.0.0.1",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/ca-certificates.crt",
        "SSL_CERT_FILE":       "/etc/ssl/certs/ca-certificates.crt",
        "REQUESTS_CA_BUNDLE":  "/etc/ssl/certs/ca-certificates.crt",
    }

    git_gate_plan = prepare_git_gate(bottle, slug)
    agent_dir, prompt_file = prepare_agent_state_dir(slug, spec)

    machine_name = f"bot-bottle-{slug}"
    if provider.template in PROVIDER_TEMPLATES:
        agent_image_ref = provider_runtime.image
    else:
        agent_image_ref = f"bot-bottle-{provider.template}:{slug}"
    agent_dockerfile_path = str(provider_obj.dockerfile)
    if provider.dockerfile:
        agent_image_ref = f"bot-bottle-{provider.template}:{slug}"
        agent_dockerfile_path = resolve_manifest_dockerfile(provider.dockerfile, spec)
    agent_provision = agent_provision_plan(
        template=provider.template,
        dockerfile=agent_dockerfile_path,
        state_dir=agent_dir,
        guest_home=guest_home,
        guest_env=guest_env,
        forward_host_credentials=provider.forward_host_credentials,
        auth_token=provider.auth_token,
        host_env=dict(os.environ),
        trusted_project_path=workspace_plan.workdir,
        label=spec.label,
        color=spec.color,
    )
    agent_provision = merge_provision_env_vars(agent_provision)

    egress_plan = prepare_egress(bottle, slug, agent_provision)
    supervise_plan = prepare_supervise(bottle, slug)

    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        bundle_subnet=subnet,
        bundle_gateway=gateway,
        bundle_ip=bundle_ip,
        machine_name=machine_name,
        agent_image_ref=agent_image_ref,
        guest_env=agent_provision.guest_env,
        prompt_file=prompt_file,
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        agent_provision=agent_provision,
        workspace_plan=workspace_plan,
    )
