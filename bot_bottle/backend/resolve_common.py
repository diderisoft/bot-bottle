"""Plan-resolution steps shared by every backend's `resolve_plan`."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..contract.agent_notes import with_operating_notes
from ..agent_provider import AgentProvisionPlan
from ..bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    egress_state_dir,
    git_gate_state_dir,
    read_metadata,
    supervise_state_dir,
    write_metadata,
)
from ..egress import Egress, EgressPlan
from ..contract.git_trailers import trailer_value
from ..git_gate import GitGate, GitGatePlan
from ..git_gate.commit_hooks import hooks_env
from ..git_gate.service import workspace_baseline
from ..manifest.schema import DEFAULT_BOTTLE
from ..workspace import WorkspacePlan
from ..lib.config_store import ConfigStore
from ..lib.log import die
from ..manifest import Manifest, ManifestBottle
from ..paths import host_db_path
from ..supervisor.plan import SupervisePlan
from . import BottleSpec


def mint_slug(spec: BottleSpec) -> str:
    """The bottle identity: the recorded UUID on resume, else a fresh UUID.
    Label is a mutable display name stored in metadata, never the identity."""
    if spec.identity:
        return spec.identity
    return bottle_identity()


def write_launch_metadata(
    slug: str, spec: BottleSpec, *, compose_project: str, backend: str,
) -> None:
    """Write launch metadata for `resume`. Rewritten on resume with a fresh
    `started_at`, keeping who started it (the host controller records that)."""
    previous = read_metadata(slug) if spec.identity else None
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
        origin=previous.origin if previous else "",
        agent_id=previous.agent_id if previous else "",
    ))


def prepare_agent_state_dir(slug: str, manifest: Manifest) -> tuple[Path, Path]:
    """Create the agent state dir and write the prompt file. Returns
    (agent_dir, prompt_file)."""
    agent = manifest.agent
    agent_dir = agent_state_dir(slug)
    agent_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = agent_dir / "prompt.txt"
    # The operator's prompt, plus bot-bottle's fixed operating notes (ADR 0003).
    prompt_file.write_text(with_operating_notes(agent.prompt or ""))
    prompt_file.chmod(0o600)
    return agent_dir, prompt_file


def prepare_git_gate(bottle: ManifestBottle, slug: str, trailer: str,
                     workspace: WorkspacePlan) -> GitGatePlan:
    """`workspace`, when it's a copied git repo, gives the trailer check's
    baseline: its unpushed commits, which push without the trailer. It's
    empty otherwise."""
    git_gate_dir = git_gate_state_dir(slug)
    git_gate_dir.mkdir(parents=True, exist_ok=True)
    baseline: list[str] = []
    if workspace.enabled and workspace.copy_contents and workspace.has_host_git_dir:
        baseline = workspace_baseline(workspace.host_path)
    return GitGate().prepare(bottle, slug, git_gate_dir, trailer=trailer, baseline=baseline)


def launch_trailer(spec: BottleSpec, slug: str) -> str:
    """The `Bot-Bottle` trailer for this launch: `<bottle>/<run>`, the bottles
    it was built from (as selected, else `DEFAULT_BOTTLE`) and its slug."""
    return trailer_value(spec.bottle_names or (DEFAULT_BOTTLE,), slug)


def with_commit_hooks_env(provision: AgentProvisionPlan) -> AgentProvisionPlan:
    """`provision` with the env that points git at the provisioned commit
    hooks (`git_gate.commit_hooks`), kept out of the preflight summary."""
    env = hooks_env(provision.guest_home, provision.guest_env)
    return replace(provision, guest_env={**provision.guest_env, **env},
                   hidden_env_names=provision.hidden_env_names | frozenset(env))


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
    """Prepare the supervise state dir. None when the bottle doesn't use
    supervise. Its queue is the orchestrator's, in its own DB."""
    if not bottle.supervise:
        return None
    supervise_state_dir(slug).mkdir(parents=True, exist_ok=True)
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
