"""build: build (or rebuild) the claude-bottle Docker image."""

from __future__ import annotations

import os

from .. import docker as docker_mod
from ._common import REPO_DIR


def cmd_build(_argv: list[str]) -> int:
    docker_mod.require_docker()
    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
    docker_mod.build_image(image, REPO_DIR)
    return 0
