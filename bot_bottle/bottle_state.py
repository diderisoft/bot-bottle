"""Per-bottle persistent state.

Holds optional per-bottle Dockerfile overrides, the transcript snapshot
the state-preservation helper saves before teardown, and the launch metadata that lets
`cli.py resume <identity>` reconstruct a bottle's spec. State
lives at:

    ~/.bot-bottle/state/<identity>/
        metadata.json     — agent_name + cwd + started_at (for resume)
        Dockerfile        — per-bottle override (absent → use repo's)
        transcript/       — last snapshotted agent state (best-effort)

When the per-bottle Dockerfile is present, the launch step builds
the agent image with a per-bottle tag (bot-bottle-rebuilt-<id>)
from this file rather than the repo's. The build context is still
the repo root so the Dockerfile can COPY bot_bottle source files
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
import socket
import string
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .paths import bot_bottle_root


# Directory layout: ~/.bot-bottle/state/<identity>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_COMMITTED_IMAGE_NAME = "committed-image"
_TRANSCRIPT_SUBDIR = "transcript"
# Per-daemon scratch subdirs. PRD 0018 chunk 2: bind-mount sources
# live here so chunk 3's `docker compose up` can find them at stable
# paths. Each daemon's `prepare()` writes config + CAs into its own
# subdir; the launch step is unchanged today (still `docker cp`).
_EGRESS_SUBDIR = "egress"
_GIT_GATE_SUBDIR = "git-gate"
_SUPERVISE_SUBDIR = "supervise"
_AGENT_SUBDIR = "agent"
_METADATA_NAME = "metadata.json"
# Live-config dir bind-mounted into the supervise daemon (read-only).
# Host's apply paths keep these files fresh so supervise's
# `list-egress-routes` MCP tool returns the current state —
# not a snapshot from launch time.
_LIVE_CONFIG_SUBDIR = "live-config"
LIVE_CONFIG_ROUTES_NAME = "routes.yaml"
LIVE_CONFIG_ALLOWLIST_NAME = "allowlist"
# Empty marker file. Session preservation writes it before teardown so
# cli.py's session-end cleanup knows to preserve the state dir for
# `cli.py resume <identity>`. Absent = clean up.
_PRESERVE_MARKER = ".preserve"

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
    from .backend.docker import util as docker_mod
    slug = docker_mod.slugify(agent_name)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_RANDOM_SUFFIX_LEN))
    return f"{slug}-{suffix}"


def globalize_slug(slug: str) -> str:
    """Return a globally-unique slug qualified with the current hostname.

    Assumes slug is a value returned from mint_slug. Use wherever a slug
    must be unique across hosts (e.g. deploy-key titles)."""
    return f"{socket.gethostname()}-{slug}"


@dataclass(frozen=True)
class BottleMetadata:
    """Persistent record of how a bottle was launched, written at
    start time and read by `cli.py resume`. Lives at
    ~/.bot-bottle/state/<identity>/metadata.json."""

    identity: str
    agent_name: str
    cwd: str           # empty string when --cwd was not passed
    copy_cwd: bool
    started_at: str    # ISO 8601 UTC
    # PRD 0018 chunk 3: derivable from identity via
    # `compose_project_name(identity)`, but persisted explicitly so
    # dashboard / cleanup / resume tooling can read it without
    # importing the compose module. Empty string for state dirs
    # written before chunk 3 (resume / inspect should fall back to
    # deriving from identity in that case).
    compose_project: str = ""
    # PRD 0040: backend name ("docker", "firecracker", "macos-container").
    # Empty string for state dirs written before PRD 0040; callers default
    # to "docker" for backward compatibility.
    backend: str = ""
    label: str = ""
    color: str = ""
    # Ordered bottle names selected at launch (issue #269). Empty tuple
    # for state dirs written before this change; resume falls back to
    # the agent's `bottle:` field in that case.
    bottle_names: tuple[str, ...] = ()


def metadata_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _METADATA_NAME


def write_metadata(metadata: BottleMetadata) -> Path:
    """Persist `metadata` to ~/.bot-bottle/state/<identity>/metadata.json.
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
    raw_typed = cast(dict[str, object], raw)
    raw_bottle_names = raw_typed.get("bottle_names", [])
    bottle_names: tuple[str, ...] = ()
    if isinstance(raw_bottle_names, list):
        bottle_names = tuple(str(n) for n in raw_bottle_names if isinstance(n, str))
    return BottleMetadata(
        identity=str(raw_typed.get("identity", identity)),
        agent_name=str(raw_typed.get("agent_name", "")),
        cwd=str(raw_typed.get("cwd", "")),
        copy_cwd=bool(raw_typed.get("copy_cwd", False)),
        started_at=str(raw_typed.get("started_at", "")),
        compose_project=str(raw_typed.get("compose_project", "")),
        backend=str(raw_typed.get("backend", "")),
        label=str(raw_typed.get("label", "")),
        color=str(raw_typed.get("color", "")),
        bottle_names=bottle_names,
    )


def bottle_state_dir(identity: str) -> Path:
    """Per-bottle state directory on the host. Created lazily by the
    write helpers; readers tolerate its absence."""
    return bot_bottle_root() / _STATE_SUBDIR / identity


def per_bottle_dockerfile_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PER_BOTTLE_DOCKERFILE_NAME


def per_bottle_dockerfile(identity: str) -> str | None:
    """Return the per-bottle Dockerfile content if present, else
    None. None means: use the provider or manifest Dockerfile."""
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


def committed_image_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _COMMITTED_IMAGE_NAME


def write_committed_image(identity: str, image_tag: str) -> Path:
    """Persist the committed image tag for `identity`. The next
    `cli.py resume <identity>` will boot from this image instead of
    rebuilding from the Dockerfile."""
    path = committed_image_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(image_tag.strip() + "\n")
    path.chmod(0o644)
    return path


def read_committed_image(identity: str) -> str | None:
    """Return the committed image tag for `identity`, or None if no
    commit has been recorded. Used by the Docker launch step to skip
    the Dockerfile build when a committed snapshot exists."""
    path = committed_image_path(identity)
    if not path.is_file():
        return None
    tag = path.read_text().strip()
    return tag or None


def per_bottle_image_tag(identity: str) -> str:
    """Image tag for a rebuilt bottle. Distinct from the base
    bot-bottle-claude:latest so per-bottle rebuilds don't collide in
    the docker image cache."""
    return f"bot-bottle-rebuilt-{identity}:latest"


def live_config_dir(identity: str) -> Path:
    """Per-bottle live-config dir. Bind-mounted read-only into the
    supervise daemon; the host's apply paths refresh the files on
    every operator approval so the agent's `list-*` MCP tools always
    return current state."""
    return bottle_state_dir(identity) / _LIVE_CONFIG_SUBDIR


def live_routes_path(identity: str) -> Path:
    return live_config_dir(identity) / LIVE_CONFIG_ROUTES_NAME


def live_allowlist_path(identity: str) -> Path:
    return live_config_dir(identity) / LIVE_CONFIG_ALLOWLIST_NAME


def write_live_config(
    identity: str, *, routes: str = "", allowlist: str = "",
) -> Path:
    """Initialise (or refresh) the live-config dir. Empty-string args
    leave the existing file alone (caller passes only what it knows).
    Returns the live-config dir path."""
    d = live_config_dir(identity)
    d.mkdir(parents=True, exist_ok=True)
    if routes:
        p = live_routes_path(identity)
        p.write_text(routes)
        p.chmod(0o644)
    if allowlist:
        p = live_allowlist_path(identity)
        p.write_text(allowlist)
        p.chmod(0o644)
    return d


def transcript_snapshot_dir(identity: str) -> Path:
    """Where agent session snapshots are kept for resume flows."""
    return bottle_state_dir(identity) / _TRANSCRIPT_SUBDIR


# --- Per-daemon scratch subdirs (PRD 0018 chunk 2) ------------------------
#
# Each daemon gets its own subdir under the bottle's state dir for
# bind-mount sources (config, CAs, hooks, etc.). Prepare-time writes
# land here; the state dir's normal cleanup (`cleanup_state`) reaps
# them along with everything else when the bottle session ends and
# nothing requested preservation.


def egress_state_dir(identity: str) -> Path:
    """State subdir for the egress daemon: routes.yaml + the
    per-bottle mitmproxy CA. Bind-mount source from chunk 3 onward."""
    return bottle_state_dir(identity) / _EGRESS_SUBDIR


def git_gate_state_dir(identity: str) -> Path:
    """State subdir for the git-gate daemon: entrypoint + hooks +
    per-upstream known_hosts. Bind-mount source from chunk 3
    onward."""
    return bottle_state_dir(identity) / _GIT_GATE_SUBDIR


def supervise_state_dir(identity: str) -> Path:
    """State subdir reserved for supervise daemon bind-mount sources.
    Runtime queue/audit rows live in the host-level bot-bottle SQLite
    database, so they survive state-dir cleanup."""
    return bottle_state_dir(identity) / _SUPERVISE_SUBDIR


def agent_state_dir(identity: str) -> Path:
    """State subdir for the agent's prepare-time scratch files: the
    env file (docker --env-file source) and the prompt file."""
    return bottle_state_dir(identity) / _AGENT_SUBDIR


# --- Preserve-on-close marker ----------------------------------------------


def preserve_marker_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PRESERVE_MARKER


def mark_preserved(identity: str) -> Path:
    """Mark this bottle's state for preservation across session
    teardown so cli.py's session-end cleanup leaves the state dir
    intact for a subsequent `cli.py resume`."""
    path = preserve_marker_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def is_preserved(identity: str) -> bool:
    return preserve_marker_path(identity).exists()


def clear_preserve_marker(identity: str) -> None:
    """Idempotent removal. Called at fresh launch (start or resume)
    so a marker left from a prior preserved session doesn't keep
    state alive past the next normal session-end."""
    try:
        preserve_marker_path(identity).unlink()
    except FileNotFoundError:
        pass


def cleanup_state(identity: str) -> None:
    """Remove the per-bottle state dir entirely. Called by cli.py
    when a bottle session ends and is_preserved(identity) is False.
    Idempotent — missing dir is success."""
    import shutil
    state_dir = bottle_state_dir(identity)
    if state_dir.is_dir():
        shutil.rmtree(state_dir, ignore_errors=True)


__all__ = [
    "BottleMetadata",
    "agent_state_dir",
    "bottle_identity",
    "globalize_slug",
    "bottle_state_dir",
    "cleanup_state",
    "clear_preserve_marker",
    "committed_image_path",
    "egress_state_dir",
    "git_gate_state_dir",
    "is_preserved",
    "mark_preserved",
    "metadata_path",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "per_bottle_image_tag",
    "preserve_marker_path",
    "read_committed_image",
    "read_metadata",
    "supervise_state_dir",
    "transcript_snapshot_dir",
    "write_committed_image",
    "write_metadata",
    "write_per_bottle_dockerfile",
]
