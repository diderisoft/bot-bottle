"""Per-bottle state under `~/.bot-bottle/state/<identity>/`: launch metadata
for `resume`, an optional per-bottle Dockerfile override, per-service scratch
dirs, and the preserve marker.

Each `start` mints a fresh identity (`bottle_identity`); `resume <identity>`
relaunches from its recorded metadata.
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

from .environment import active_name, is_live
from .paths import bot_bottle_root
from .util import slugify


# Directory layout: ~/.bot-bottle/state/<identity>/...
_STATE_SUBDIR = "state"
_PER_BOTTLE_DOCKERFILE_NAME = "Dockerfile"
_COMMITTED_IMAGE_NAME = "committed-image"
_COMMITTED_ROOTFS_NAME = "committed-rootfs.tar"
# Per-service scratch subdirs.
_EGRESS_SUBDIR = "egress"
_GIT_GATE_SUBDIR = "git-gate"
_SUPERVISE_SUBDIR = "supervise"
_AGENT_SUBDIR = "agent"
_METADATA_NAME = "metadata.json"
# Empty marker: preserve the state dir at session end for `resume`.
_PRESERVE_MARKER = ".preserve"

# 5 base36 chars, ~60M combinations.
_RANDOM_SUFFIX_LEN = 5
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def bottle_identity(agent_name: str) -> str:
    """Mint a fresh, random per-launch identity: `slugify(agent_name)` plus a
    5-char suffix so concurrent starts don't collide."""
    slug = slugify(agent_name)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_RANDOM_SUFFIX_LEN))
    return f"{slug}-{suffix}"


def globalize_slug(slug: str) -> str:
    """`slug` qualified with this hostname, and the environment's name in a
    named one, for names that must be unique across hosts and the
    environments on them (e.g. deploy-key titles)."""
    env = active_name()
    where = socket.gethostname() if is_live(env) else f"{socket.gethostname()}-{env}"
    return f"{where}-{slug}"


@dataclass(frozen=True)
class BottleMetadata:
    """How a bottle was launched; read by `resume`."""

    identity: str
    agent_name: str
    cwd: str  # empty when the workspace wasn't copied or mounted
    copy_cwd: bool
    started_at: str  # ISO 8601 UTC
    # Derivable from identity; stored so tooling needn't import compose. Empty
    # in older state dirs.
    compose_project: str = ""
    # Empty in older state dirs; callers default to "docker".
    backend: str = ""
    label: str = ""
    color: str = ""
    # Empty in older state dirs; resume falls back to the agent's `bottle:`.
    bottle_names: tuple[str, ...] = ()


def metadata_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _METADATA_NAME


def write_metadata(metadata: BottleMetadata) -> Path:
    """Write `metadata.json` (0o644; holds no secrets)."""
    path = metadata_path(metadata.identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataclasses.asdict(metadata), indent=2) + "\n")
    path.chmod(0o644)
    return path


def read_metadata(identity: str) -> BottleMetadata | None:
    """The recorded metadata for `identity`, or None."""
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
        # A bottle started before the mount was removed resumes with a copy.
        copy_cwd=bool(raw_typed.get("copy_cwd", False) or raw_typed.get("mount_cwd", False)),
        started_at=str(raw_typed.get("started_at", "")),
        compose_project=str(raw_typed.get("compose_project", "")),
        backend=str(raw_typed.get("backend", "")),
        label=str(raw_typed.get("label", "")),
        color=str(raw_typed.get("color", "")),
        bottle_names=bottle_names,
    )


def bottle_state_dir(identity: str) -> Path:
    """The bottle's state dir. Created lazily by writers; readers tolerate
    absence."""
    return bot_bottle_root() / _STATE_SUBDIR / identity


def per_bottle_dockerfile_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PER_BOTTLE_DOCKERFILE_NAME


def per_bottle_dockerfile(identity: str) -> str | None:
    """The per-bottle Dockerfile override, or None."""
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


def committed_rootfs_path(identity: str) -> Path:
    """Where the Firecracker freezer stores the bottle's rootfs snapshot (a
    tar), which `resume` boots from."""
    return bottle_state_dir(identity) / _COMMITTED_ROOTFS_NAME


def write_committed_image(identity: str, image_tag: str) -> Path:
    """Record the committed image for `identity`; `resume` boots from it."""
    path = committed_image_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(image_tag.strip() + "\n")
    path.chmod(0o644)
    return path


def read_committed_image(identity: str) -> str | None:
    """The committed image tag for `identity`, or None."""
    path = committed_image_path(identity)
    if not path.is_file():
        return None
    tag = path.read_text().strip()
    return tag or None


# Per-service scratch subdirs, removed with the state dir by `cleanup_state`.


def egress_state_dir(identity: str) -> Path:
    """Egress config, including the staged gateway CA (`gateway-ca/`)."""
    return bottle_state_dir(identity) / _EGRESS_SUBDIR


def git_gate_state_dir(identity: str) -> Path:
    """git-gate entrypoint, hooks and per-upstream known_hosts."""
    return bottle_state_dir(identity) / _GIT_GATE_SUBDIR


def supervise_state_dir(identity: str) -> Path:
    """Reserved for supervise mount sources; queue rows live in the host DB."""
    return bottle_state_dir(identity) / _SUPERVISE_SUBDIR


def agent_state_dir(identity: str) -> Path:
    """The agent's env file and prompt file."""
    return bottle_state_dir(identity) / _AGENT_SUBDIR




def preserve_marker_path(identity: str) -> Path:
    return bottle_state_dir(identity) / _PRESERVE_MARKER


def mark_preserved(identity: str) -> Path:
    """Keep this bottle's state dir at session end for `resume`."""
    path = preserve_marker_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def is_preserved(identity: str) -> bool:
    return preserve_marker_path(identity).exists()


def clear_preserve_marker(identity: str) -> None:
    """Remove a stale marker at launch. Idempotent."""
    try:
        preserve_marker_path(identity).unlink()
    except FileNotFoundError:
        pass


def cleanup_state(identity: str) -> None:
    """Remove the bottle's state dir. Idempotent."""
    import shutil
    state_dir = bottle_state_dir(identity)
    if state_dir.is_dir():
        shutil.rmtree(state_dir, ignore_errors=True)


def capture_session_state(identity: str, exit_code: int) -> None:
    """After an agent's session ends: keep its state (and its home) for
    `resume` if the agent exited non-zero, as a stop does."""
    if identity and exit_code != 0:
        mark_preserved(identity)


def settle_state(identity: str) -> None:
    """After teardown: print the resume hint if preserved, else remove the
    state dir."""
    from .lib.log import info  # pylint: disable=import-outside-toplevel
    if not identity:
        return
    if is_preserved(identity):
        info(f"to resume this bottled agent: bot-bottle resume {identity}")
        return
    cleanup_state(identity)


__all__ = [
    "BottleMetadata",
    "capture_session_state",
    "settle_state",
    "agent_state_dir",
    "bottle_identity",
    "globalize_slug",
    "bottle_state_dir",
    "cleanup_state",
    "clear_preserve_marker",
    "committed_image_path",
    "committed_rootfs_path",
    "egress_state_dir",
    "git_gate_state_dir",
    "is_preserved",
    "mark_preserved",
    "metadata_path",
    "per_bottle_dockerfile",
    "per_bottle_dockerfile_path",
    "preserve_marker_path",
    "read_committed_image",
    "read_metadata",
    "supervise_state_dir",
    "write_committed_image",
    "write_metadata",
    "write_per_bottle_dockerfile",
]
