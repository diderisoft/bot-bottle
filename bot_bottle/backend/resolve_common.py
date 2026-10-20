"""Shared helpers used across backends' resolve_plan steps.

Each helper owns one well-defined step of the per-bottle plan
resolution so the backends don't repeat the same logic.
Backend-specific steps (container names, env-file, per-bottle
Dockerfile overrides, subnet allocation) stay in the backend's own
resolve_plan.py.
"""

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
from ..lib.log import die
from ..manifest import Manifest, ManifestBottle
from ..orchestrator.store.store_manager import StoreManager
from ..paths import host_db_path
from ..supervisor.plan import SupervisePlan
from ..util import slugify
from . import BottleSpec


def mint_slug(spec: BottleSpec) -> str:
    """Return the bottle identity: the recorded identity for a resume,
    or a freshly minted one for a new start.

    When a label is provided it becomes the full slug (no random suffix),
    so two launches with the same label collide by design.  When no label
    is given the identity is minted with a random suffix to avoid
    collisions between anonymous launches of the same agent."""
    if spec.identity:
        return spec.identity
    if spec.label:
        return slugify(spec.label)
    return bottle_identity(spec.agent_name)


def write_launch_metadata(
    slug: str, spec: BottleSpec, *, compose_project: str, backend: str,
) -> None:
    """Persist launch metadata so `cli.py resume <identity>` can
    reconstruct the spec. Idempotent — re-writes on resume with a
    refreshed started_at."""
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
    """Create the agent state subdir, write the prompt file.
    Returns (agent_dir, prompt_file)."""
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
) -> EgressPlan:
    egress_dir = egress_state_dir(slug)
    egress_dir.mkdir(parents=True, exist_ok=True)
    return Egress().prepare(bottle, slug, egress_dir, provision.egress_routes)


def prepare_supervise(bottle: ManifestBottle, slug: str) -> SupervisePlan | None:
    """Prepare the supervise daemon state dir and migrate the host database.
    Returns None when bottle.supervise is falsy. `internal_network` is set by
    the launch step before `.start`."""
    if not bottle.supervise:
        return None
    supervise_state_dir(slug).mkdir(parents=True, exist_ok=True)
    mgr = StoreManager(host_db_path())
    mgr.migrate()
    return SupervisePlan(slug=slug, db_path=mgr.db_path)


def merge_provision_env_vars(provision: AgentProvisionPlan) -> AgentProvisionPlan:
    """Fold provision.env_vars into guest_env (setdefault semantics)
    and return a new plan with the merged guest_env."""
    merged = dict(provision.guest_env)
    for key, val in provision.env_vars.items():
        merged.setdefault(key, val)
    return replace(provision, guest_env=merged)


def reject_nested_containers(backend: str, manifest: Manifest) -> None:
    """Fail loudly when a backend cannot honor `nested_containers: true`.

    Silently ignoring it would hand the agent a bottle where `docker` is not
    there — and the only sound alternatives on these backends (a host daemon
    socket, a privileged container) are exactly what issue #392 rules out.
    """
    if not manifest.bottle.nested_containers:
        return
    die(
        f"nested_containers is not supported on the {backend} backend. "
        "Only macos-container runs a guest-local container engine today; "
        "mounting the host Docker socket is not an option bot-bottle offers."
    )


def resolve_manifest_dockerfile(path_value: str, spec: BottleSpec) -> str:
    """Resolve a manifest-supplied dockerfile path relative to user_cwd."""
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
