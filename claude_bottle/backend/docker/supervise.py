"""DockerSupervise — the Docker-specific lifecycle for the per-bottle
supervise sidecar (PRD 0013). Inherits the platform-agnostic prepare
step (queue dir + current-config staging) from `Supervise`."""

from __future__ import annotations

import os
from pathlib import Path

from ...supervise import (
    SUPERVISE_HOSTNAME,
    SUPERVISE_PORT,
    Supervise,
)
from . import util as docker_mod


SUPERVISE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_SUPERVISE_IMAGE",
    "claude-bottle-supervise:latest",
)

SUPERVISE_DOCKERFILE = "Dockerfile.supervise"

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def supervise_container_name(slug: str) -> str:
    return f"claude-bottle-supervise-{slug}"


def supervise_url() -> str:
    """Base URL the agent's MCP client dials. Stable across bottles
    because the sidecar attaches `--network-alias supervise` on the
    internal network."""
    return f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}"


def build_supervise_image() -> None:
    """Build the supervise image from `Dockerfile.supervise`. Called
    by `DockerSupervise.start`; exposed at module level so tests can
    build it without running the full launch pipeline."""
    docker_mod.build_image(SUPERVISE_IMAGE, _REPO_DIR, dockerfile=SUPERVISE_DOCKERFILE)


class DockerSupervise(Supervise):
    """Docker-flavored Supervise: inherits `.prepare()` from the base.
    Container lifecycle is owned by compose; per-container
    `.start()` / `.stop()` were removed in PRD 0024 chunk 3."""
