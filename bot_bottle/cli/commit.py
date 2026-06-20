"""commit: freeze a running Docker bottle's container state to a local image.

Runs `docker commit <container> <image-tag>` on the active agent
container and stores the image tag in per-bottle state so the next
`./cli.py resume <slug>` boots from that snapshot instead of
rebuilding from the Dockerfile.

Only the Docker backend is supported. Smolmachines VMs have no
container-level commit API in the current smolvm CLI surface.
"""

from __future__ import annotations

import argparse

from ..backend import enumerate_active_agents
from ..backend.docker.util import commit_container
from ..bottle_state import mark_preserved, read_metadata, write_committed_image
from ..log import die, info
from ._common import PROG
from . import tui


_COMMITTED_IMAGE_PREFIX = "bot-bottle-committed-"
_DOCKER_BACKENDS = {"docker", ""}


def _committed_image_tag(slug: str) -> str:
    return f"{_COMMITTED_IMAGE_PREFIX}{slug}:latest"


def _agent_container_name(slug: str) -> str:
    return f"bot-bottle-{slug}"


def cmd_commit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} commit", add_help=True)
    parser.add_argument(
        "slug",
        nargs="?",
        default=None,
        help=(
            "bottle slug from `cli.py list active` "
            "(omit to pick interactively)"
        ),
    )
    args = parser.parse_args(argv)

    slug = args.slug
    if slug is None:
        active = enumerate_active_agents()
        if not active:
            die("no active bottles; start one with `./cli.py start`")
        choices = [a.slug for a in active]
        slug = tui.filter_select(choices, title="Select bottle to commit")
        if slug is None:
            return 0

    metadata = read_metadata(slug)
    backend = metadata.backend if metadata else ""
    if backend not in _DOCKER_BACKENDS:
        die(
            f"commit is only supported for the docker backend; "
            f"bottle {slug!r} uses {backend!r}"
        )

    container = _agent_container_name(slug)
    image_tag = _committed_image_tag(slug)

    commit_container(container, image_tag)
    write_committed_image(slug, image_tag)
    mark_preserved(slug)
    info(f"to resume from this snapshot: ./cli.py resume {slug}")
    info(f"to export for migration:      docker save {image_tag} -o {slug}.tar")
    return 0
