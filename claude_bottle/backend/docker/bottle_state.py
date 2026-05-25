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

from pathlib import Path

from ... import supervise as _supervise


# Directory layout: ~/.claude-bottle/state/<slug>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_TRANSCRIPT_SUBDIR = "transcript"


def bottle_state_dir(slug: str) -> Path:
    """Per-bottle state directory on the host. Created lazily by the
    write helpers; readers tolerate its absence."""
    return _supervise.claude_bottle_root() / _STATE_SUBDIR / slug


def per_bottle_dockerfile_path(slug: str) -> Path:
    return bottle_state_dir(slug) / _PER_BOTTLE_DOCKERFILE_NAME


def per_bottle_dockerfile(slug: str) -> str | None:
    """Return the per-bottle Dockerfile content if present, else
    None. None means: use the repo's Dockerfile (the original
    pre-capability-block behavior)."""
    p = per_bottle_dockerfile_path(slug)
    if p.is_file():
        return p.read_text()
    return None


def write_per_bottle_dockerfile(slug: str, content: str) -> Path:
    p = per_bottle_dockerfile_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    p.chmod(0o644)
    return p


def per_bottle_image_tag(slug: str) -> str:
    """Image tag for a rebuilt bottle. Distinct from the base
    claude-bottle:latest so per-bottle rebuilds don't collide in
    the docker image cache."""
    return f"claude-bottle-rebuilt-{slug}:latest"


def transcript_snapshot_dir(slug: str) -> Path:
    """Where capability_apply stashes the agent's transcript before
    teardown, so the next `cli.py start <agent>` can offer to
    resume from it."""
    return bottle_state_dir(slug) / _TRANSCRIPT_SUBDIR


__all__ = [
    "bottle_state_dir",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "per_bottle_image_tag",
    "transcript_snapshot_dir",
    "write_per_bottle_dockerfile",
]
