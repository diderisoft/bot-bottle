"""Per-bottle persistent state (PRD 0016).

Holds the per-bottle Dockerfile override that capability-block
remediation writes, the transcript snapshot the state-preservation
helper saves before teardown, and the launch metadata that lets
`cli.py resume <identity>` reconstruct a bottle's spec. State
lives at:

    ~/.claude-bottle/state/<identity>/
        metadata.json     — agent_name + cwd + started_at (for resume)
        Dockerfile        — per-bottle override (absent → use repo's)
        transcript/       — last snapshotted agent state (best-effort)

When the per-bottle Dockerfile is present, the launch step builds
the agent image with a per-bottle tag (claude-bottle-rebuilt-<id>)
from this file rather than the repo's. The build context is still
the repo root so the Dockerfile can COPY claude_bottle source files
the same way the original does.

Identity model:
- Every `cli.py start <agent>` mints a fresh identity via
  `bottle_identity(agent_name)`: slug-prefix for readability plus a
  5-char random suffix for parallel-safe uniqueness. The metadata
  written at launch time pins (agent_name, cwd) to that identity.
- `cli.py resume <identity>` reads the metadata and re-launches a
  bottle pinned to the same identity, picking up any per-bottle
  Dockerfile and transcript snapshot.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from ... import supervise as _supervise
from . import util as docker_mod


# Directory layout: ~/.claude-bottle/state/<identity>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_TRANSCRIPT_SUBDIR = "transcript"
_METADATA_NAME = "metadata.json"

# 5 chars of base36 alphabet ≈ 60M combinations. Plenty for human
# operators starting bottles by hand; collision-free in practice.
_RANDOM_SUFFIX_LEN = 5
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def bottle_identity(agent_name: str) -> str:
    """Mint a fresh per-launch bottle identity. The slug-prefix is
    `slugify(agent_name)` for readability; the suffix is 5 random
    base36 chars so two simultaneous `start <agent>` invocations
    don't collide on container/network names.

    Every call produces a different identity (non-deterministic).
    To continue an existing bottle's state, use the recorded
    identity from BottleMetadata via `cli.py resume <identity>`,
    not this function."""
    slug = docker_mod.slugify(agent_name)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_RANDOM_SUFFIX_LEN))
    return f"{slug}-{suffix}"


@dataclass(frozen=True)
class BottleMetadata:
    """Persistent record of how a bottle was launched, written at
    start time and read by `cli.py resume`. Lives at
    ~/.claude-bottle/state/<identity>/metadata.json."""

    identity: str
    agent_name: str
    cwd: str           # empty string when --cwd was not passed
    copy_cwd: bool
    started_at: str    # ISO 8601 UTC


def metadata_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _METADATA_NAME


def write_metadata(metadata: BottleMetadata) -> Path:
    """Persist `metadata` to ~/.claude-bottle/state/<identity>/metadata.json.
    Mode 0o644 — no secrets, just (agent_name, cwd, timestamp)."""
    path = metadata_path(metadata.identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(metadata), indent=2) + "\n")
    path.chmod(0o644)
    return path


def read_metadata(identity: str) -> BottleMetadata | None:
    """Return the metadata for `identity`, or None if no state has
    been recorded for it. Used by `cli.py resume` to reconstruct
    the launch spec."""
    path = metadata_path(identity)
    if not path.is_file():
        return None
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        return None
    return BottleMetadata(
        identity=str(raw.get("identity", identity)),
        agent_name=str(raw.get("agent_name", "")),
        cwd=str(raw.get("cwd", "")),
        copy_cwd=bool(raw.get("copy_cwd", False)),
        started_at=str(raw.get("started_at", "")),
    )


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
    "BottleMetadata",
    "bottle_identity",
    "bottle_state_dir",
    "metadata_path",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "per_bottle_image_tag",
    "read_metadata",
    "transcript_snapshot_dir",
    "write_metadata",
    "write_per_bottle_dockerfile",
]
