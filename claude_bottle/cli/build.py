"""build: build (or rebuild) the claude-bottle Docker image."""

from __future__ import annotations

import argparse
import os

from .. import docker as docker_mod
from ._common import PROG, REPO_DIR


def cmd_build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} build", add_help=True)
    parser.parse_args(argv)

    docker_mod.require_docker()
    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
    docker_mod.build_image(image, REPO_DIR)
    return 0
