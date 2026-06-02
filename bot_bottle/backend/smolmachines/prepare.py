"""smolmachines `_resolve_plan` (PRD 0023 chunks 2d + 4c).

Resolves the per-bottle docker subnet + bundle IP and assembles
the guest env. The agent's docker image build → smolmachine
pack pipeline runs in `launch.launch`, not here, so the
dashboard's preflight modal isn't garbled by docker-build output
before the operator has confirmed.

No VM bringup — that's `launch.launch`'s job."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from ...agent_provider import agent_provision_plan, runtime_for
from ...backend import BottleSpec
from ...backend.docker.bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    egress_state_dir,
    git_gate_state_dir,
    pipelock_state_dir,
    supervise_state_dir,
    write_metadata,
)
from ...egress import Egress
from ...env import resolve_env
from ...git_gate import GitGate
from ...pipelock import PipelockProxy
from ...supervise import Supervise
from .bottle_plan import SmolmachinesBottlePlan
from .util import smolmachines_bundle_subnet, smolmachines_preflight


# Gateway ports the bundle exposes inside its container — pipelock
# HTTPS proxy, git-gate's git-daemon, supervise's MCP. The agent
# inside the smolvm guest dials these on the bundle's pinned IP.
_BUNDLE_PIPELOCK_PORT = 8888
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
    provider_runtime = runtime_for(provider.template)

    slug = spec.identity or bottle_identity(spec.agent_name)

    # Record minimal metadata so `cli.py resume` can recover the
    # slug. Same schema as the docker backend.
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        # No compose project for smolmachines bottles; chunk 4
        # will give dashboard discovery a backend-specific path.
        compose_project="",
    ))

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

    git_gate_dir = git_gate_state_dir(slug)
    git_gate_dir.mkdir(parents=True, exist_ok=True)
    git_gate_plan = GitGate().prepare(bottle, slug, git_gate_dir)

    # Prompt file is always written (mode 0o600) so the in-VM
    # path always exists. Content is the agent's `prompt`
    # field (markdown body) — empty for agents with no prompt.
    # claude-code reads it via --append-system-prompt-file only
    # when non-empty, but the file must exist either way to
    # match the docker backend's contract.
    agent_dir = agent_state_dir(slug)
    agent_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = agent_dir / "prompt.txt"
    agent = manifest.agents[spec.agent_name]
    prompt_file.write_text(agent.prompt or "")
    prompt_file.chmod(0o600)

    machine_name = f"bot-bottle-{slug}"
    # Stash the agent image ref — `launch.launch` runs the
    # build → pack pipeline at bringup. Honors BOT_BOTTLE_IMAGE
    # to match the docker backend's `resolve_plan` default.
    agent_dockerfile_path = ""
    if provider.dockerfile:
        agent_dockerfile_path = _resolve_manifest_dockerfile(provider.dockerfile, spec)
        image_default = f"bot-bottle-{provider.template}:{slug}"
    elif provider_runtime.dockerfile:
        agent_dockerfile_path = provider_runtime.dockerfile
        image_default = provider_runtime.image
    else:
        image_default = provider_runtime.image
    agent_image_ref = os.environ.get("BOT_BOTTLE_IMAGE", image_default)
    agent_provision = agent_provision_plan(
        template=provider.template,
        dockerfile=agent_dockerfile_path,
        state_dir=agent_dir,
        guest_home=os.environ.get("BOT_BOTTLE_GUEST_HOME", "/home/node"),
        guest_env=guest_env,
        forward_host_credentials=provider.forward_host_credentials,
        auth_token=provider.auth_token,
        host_env=dict(os.environ),
    )
    merged_guest_env = dict(agent_provision.guest_env)
    for key, val in agent_provision.env_vars.items():
        merged_guest_env.setdefault(key, val)
    agent_provision = replace(agent_provision, guest_env=merged_guest_env)

    # Inner Plans for the four bundle daemons. The ABCs are
    # platform-neutral — `.prepare()` writes config files + returns
    # a Plan dataclass with no backend-specific assumptions. State
    # dirs are still keyed by slug under the docker backend's
    # bottle_state layout (shared on-host convention; not a docker
    # dependency).
    pipelock_dir = pipelock_state_dir(slug)
    pipelock_dir.mkdir(parents=True, exist_ok=True)
    proxy_plan = PipelockProxy().prepare(
        bottle, slug, pipelock_dir, agent_provision.egress_routes,
    )

    egress_dir = egress_state_dir(slug)
    egress_dir.mkdir(parents=True, exist_ok=True)
    egress_plan = Egress().prepare(
        bottle, slug, egress_dir, agent_provision.egress_routes,
    )

    supervise_plan = None
    if bottle.supervise:
        supervise_dir = supervise_state_dir(slug)
        supervise_dir.mkdir(parents=True, exist_ok=True)
        supervise_plan = Supervise().prepare(slug, supervise_dir)

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
        proxy_plan=proxy_plan,
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        agent_provision=agent_provision,
    )


def _resolve_manifest_dockerfile(path_value: str, spec: BottleSpec) -> str:
    path = Path(os.path.expanduser(path_value))
    if not path.is_absolute():
        path = Path(spec.user_cwd) / path
    return str(path)
