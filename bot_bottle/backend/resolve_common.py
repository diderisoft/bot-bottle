"""Plan-resolution steps shared by every backend's `resolve_plan`."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..agent_provider import AgentProvisionPlan
from ..bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    egress_state_dir,
    git_gate_state_dir,
    supervise_state_dir,
    write_metadata,
)
from ..egress import Egress, EgressPlan
from ..git_gate import GitGate, GitGatePlan
from ..lib.config_store import ConfigStore
from ..lib.log import die
from ..manifest import Manifest, ManifestBottle
from ..orchestrator.store.store_manager import StoreManager
from ..paths import host_db_path
from ..supervisor.plan import SupervisePlan
from ..util import slugify
from . import BottleSpec


def mint_slug(spec: BottleSpec) -> str:
    """The bottle identity: the recorded one on resume, else the label as-is
    (so equal labels collide by design), else a freshly minted one."""
    if spec.identity:
        return spec.identity
    if spec.label:
        return slugify(spec.label)
    return bottle_identity(spec.agent_name)


def write_launch_metadata(
    slug: str, spec: BottleSpec, *, compose_project: str, backend: str,
) -> None:
    """Write launch metadata for `resume`. Rewritten on resume with a fresh
    `started_at`."""
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        compose_project=compose_project,
        backend=backend,
        label=spec.label,
        color=spec.color,
        bottle_names=spec.bottle_names,
    ))


def prepare_agent_state_dir(slug: str, manifest: Manifest) -> tuple[Path, Path]:
    """Create the agent state dir and write the prompt file. Returns
    (agent_dir, prompt_file)."""
    agent = manifest.agent
    agent_dir = agent_state_dir(slug)
    agent_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = agent_dir / "prompt.txt"
    prompt_file.write_text(agent.prompt or "")
    prompt_file.chmod(0o600)
    return agent_dir, prompt_file


def prepare_git_gate(bottle: ManifestBottle, slug: str) -> GitGatePlan:
    git_gate_dir = git_gate_state_dir(slug)
    git_gate_dir.mkdir(parents=True, exist_ok=True)
    return GitGate().prepare(bottle, slug, git_gate_dir)


def prepare_egress(
    bottle: ManifestBottle, slug: str, provision: AgentProvisionPlan,
    owner: str = "",
) -> EgressPlan:
    """`owner` names the agent and bottle in a same-host route error."""
    egress_dir = egress_state_dir(slug)
    egress_dir.mkdir(parents=True, exist_ok=True)
    return Egress().prepare(
        bottle, slug, egress_dir, provision.egress_routes,
        default_audit_delivery=ConfigStore(host_db_path()).audit_delivery(), owner=owner)


def prepare_supervise(bottle: ManifestBottle, slug: str) -> SupervisePlan | None:
    """Prepare the supervise state dir and migrate the host DB. None when the
    bottle doesn't use supervise."""
    if not bottle.supervise:
        return None
    supervise_state_dir(slug).mkdir(parents=True, exist_ok=True)
    StoreManager(host_db_path()).migrate()
    return SupervisePlan(slug=slug)


def merge_provision_env_vars(provision: AgentProvisionPlan) -> AgentProvisionPlan:
    """Merge `provision.env_vars` into `guest_env` (existing keys win)."""
    merged = dict(provision.guest_env)
    for key, val in provision.env_vars.items():
        merged.setdefault(key, val)
    return replace(provision, guest_env=merged)


def reject_nested_containers(backend: str, manifest: Manifest) -> None:
    """Fail when a backend can't honor `nested_containers: true`, rather than
    hand the agent a bottle without `docker`.
    """
    if not manifest.bottle.nested_containers:
        return
    die(
        f"nested_containers is not supported on the {backend} backend. "
        "Only macos-container runs a guest-local container engine today; "
        "mounting the host Docker socket is not an option bot-bottle offers."
    )


def resolve_manifest_dockerfile(path_value: str, spec: BottleSpec) -> str:
    """A manifest dockerfile path, resolved relative to `user_cwd`."""
    path = Path(os.path.expanduser(path_value))
    if not path.is_absolute():
        path = Path(spec.user_cwd) / path
    return str(path)


__all__ = [
    "merge_provision_env_vars",
    "reject_nested_containers",
    "mint_slug",
    "prepare_agent_state_dir",
    "prepare_egress",
    "prepare_git_gate",
    "prepare_supervise",
    "resolve_manifest_dockerfile",
    "write_launch_metadata",
]
