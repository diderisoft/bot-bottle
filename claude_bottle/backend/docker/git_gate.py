"""DockerGitGate — the Docker-specific lifecycle for the per-agent
git-gate sidecar (PRD 0008). Inherits the platform-agnostic prepare
step (upstream lift + entrypoint/hook render) from `GitGate`."""

from __future__ import annotations

import os
from pathlib import Path

from ...git_gate import GitGate
from . import util as docker_mod


GIT_GATE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_GIT_GATE_IMAGE",
    "claude-bottle-git-gate:latest",
)

GIT_GATE_DOCKERFILE = "Dockerfile.git-gate"

GIT_GATE_ENTRYPOINT_IN_CONTAINER = "/git-gate-entrypoint.sh"
GIT_GATE_HOOK_IN_CONTAINER = "/etc/git-gate/pre-receive"
GIT_GATE_ACCESS_HOOK_IN_CONTAINER = "/etc/git-gate/access-hook"
GIT_GATE_CREDS_DIR_IN_CONTAINER = "/git-gate/creds"

# git daemon's default listening port. Surfaced as a constant because
# integration tests probe the gate on it.
GIT_GATE_PORT = 9418

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/git_gate.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def git_gate_container_name(slug: str) -> str:
    return f"claude-bottle-git-gate-{slug}"


def git_gate_host(slug: str) -> str:
    """The hostname the agent's git client should connect to (same as
    the container name — Docker's embedded DNS resolves it on the
    `--internal` network)."""
    return git_gate_container_name(slug)


def build_git_gate_image() -> None:
    """Build the git-gate image from `Dockerfile.git-gate`. Called by
    `DockerGitGate.start`; exposed at module level so integration
    tests can build it without running the full launch pipeline."""
    docker_mod.build_image(GIT_GATE_IMAGE, _REPO_DIR, dockerfile=GIT_GATE_DOCKERFILE)


class DockerGitGate(GitGate):
    """Docker-flavored GitGate: inherits `.prepare()` from the base.
    Container lifecycle is owned by compose; per-container
    `.start()` / `.stop()` were removed in PRD 0024 chunk 3."""
