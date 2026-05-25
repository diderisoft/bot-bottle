"""Per-bottle persistent state (PRD 0016).

Holds the per-bottle Dockerfile override that capability-block
remediation writes, plus the transcript snapshot the
state-preservation helper saves before teardown. State lives at:

    ~/.claude-bottle/state/<slug>/
        Dockerfile        — per-bottle override (absent → use repo's)
        transcript/       — last snapshotted agent state (best-effort)

When the per-bottle Dockerfile is present, the launch step builds
the agent image with a per-bottle tag (claude-bottle-rebuilt-<slug>)
from this file rather than the repo's. The build context is still
the repo root so the Dockerfile can COPY claude_bottle source files
the same way the original does.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ... import supervise as _supervise
from . import util as docker_mod


# Directory layout: ~/.claude-bottle/state/<identity>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_TRANSCRIPT_SUBDIR = "transcript"

# How many hex chars of the cwd hash to fold into the identity. 12
# hex chars = 48 bits of entropy; the cost of a collision is two
# unrelated cwds sharing the same state — annoying but not security-
# relevant. 12 keeps the identity short enough to stay readable in
# container names and `ls` output.
_CWD_HASH_LEN = 12


def bottle_identity(agent_name: str, cwd: Path | None) -> str:
    """Stable, unique identifier for a bottle. Used as the key for
    every persistent and runtime resource: container names, network
    names, queue dir, audit log, per-bottle Dockerfile state.

    Without --cwd, the identity is just `slugify(agent_name)` — the
    same value the codebase used to compute as `slug`. With --cwd
    the identity is `slugify(agent_name)-<sha256(resolved-cwd)[:N]>`
    so the same agent against different projects gets distinct
    state. Same agent against the same cwd is stable across launches.

    `cwd` should be the path the agent will see, *resolved* by the
    caller, or None when no cwd was passed (no --cwd flag)."""
    slug = docker_mod.slugify(agent_name)
    if cwd is None:
        return slug
    h = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()
    return f"{slug}-{h[:_CWD_HASH_LEN]}"


def bottle_state_dir(identity: str) -> Path:
    """Per-bottle state directory on the host. Created lazily by the
    write helpers; readers tolerate its absence."""
    return _supervise.claude_bottle_root() / _STATE_SUBDIR / identity


def per_bottle_dockerfile_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PER_BOTTLE_DOCKERFILE_NAME


def per_bottle_dockerfile(identity: str) -> str | None:
    """Return the per-bottle Dockerfile content if present, else
    None. None means: use the repo's Dockerfile (the original
    pre-capability-block behavior)."""
    p = per_bottle_dockerfile_path(identity)
    if p.is_file():
        return p.read_text()
    return None


def write_per_bottle_dockerfile(identity: str, content: str) -> Path:
    p = per_bottle_dockerfile_path(identity)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(0o644)
    return p


def per_bottle_image_tag(identity: str) -> str:
    """Image tag for a rebuilt bottle. Distinct from the base
    claude-bottle:latest so per-bottle rebuilds don't collide in
    the docker image cache."""
    return f"claude-bottle-rebuilt-{identity}:latest"


def transcript_snapshot_dir(identity: str) -> Path:
    """Where capability_apply stashes the agent's transcript before
    teardown, so the next `cli.py start <agent>` can offer to
    resume from it."""
    return bottle_state_dir(identity) / _TRANSCRIPT_SUBDIR


__all__ = [
    "bottle_identity",
    "bottle_state_dir",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "per_bottle_image_tag",
    "transcript_snapshot_dir",
    "write_per_bottle_dockerfile",
]
