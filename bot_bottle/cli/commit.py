"""commit: freeze a running bottle's state to a resumable artifact.

Docker bottles are committed to a local Docker image. Macos-container
bottles are exported and rebuilt as a local Apple Container image.
Smolmachines bottles are packed from the running VM into a
`.smolmachine` artifact. The resulting reference is stored in
per-bottle state so the next `./cli.py resume <slug>` boots from the
snapshot instead of rebuilding from the Dockerfile.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..backend import enumerate_active_agents
from ..backend.docker.util import commit_container as docker_commit_container
from ..backend.macos_container.util import commit_container as macos_commit_container
from ..backend.macos_container.util import container_is_running as macos_container_is_running
from ..backend.macos_container.util import stop_container as macos_stop_container
from ..backend.smolmachines.smolvm import pack_create_from_vm
from ..bottle_state import bottle_state_dir
from ..bottle_state import mark_preserved, read_metadata, write_committed_image
from ..log import die, info
from ._common import PROG, read_tty_line
from . import tui


_COMMITTED_IMAGE_PREFIX = "bot-bottle-committed-"
_DOCKER_BACKENDS = {"docker", ""}
_MACOS_CONTAINER_BACKEND = "macos-container"
_SMOLMACHINES_BACKEND = "smolmachines"


def _committed_image_tag(slug: str) -> str:
    return f"{_COMMITTED_IMAGE_PREFIX}{slug}:latest"


def _agent_container_name(slug: str) -> str:
    return f"bot-bottle-{slug}"


def _agent_machine_name(slug: str) -> str:
    return f"bot-bottle-{slug}"


def _committed_smolmachine_output(slug: str) -> Path:
    return bottle_state_dir(slug) / "committed-smolmachine"


def _committed_smolmachine_artifact(slug: str) -> Path:
    output = _committed_smolmachine_output(slug)
    return output.with_name(f"{output.name}.smolmachine")


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
    if backend in _DOCKER_BACKENDS:
        container = _agent_container_name(slug)
        image_tag = _committed_image_tag(slug)

        docker_commit_container(container, image_tag)
        write_committed_image(slug, image_tag)
        mark_preserved(slug)
        info(f"to resume from this snapshot: ./cli.py resume {slug}")
        info(f"to export for migration:      docker save {image_tag} -o {slug}.tar")
        return 0

    if backend == _MACOS_CONTAINER_BACKEND:
        container = _agent_container_name(slug)
        image_tag = _committed_image_tag(slug)

        if macos_container_is_running(container):
            sys.stderr.write(
                f"bot-bottle: bottle {slug!r} is running; "
                "commit will stop it. Continue? [y/N] "
            )
            sys.stderr.flush()
            reply = read_tty_line().strip().lower()
            if reply not in ("y", "yes"):
                return 0
            macos_stop_container(container)

        macos_commit_container(container, image_tag)
        write_committed_image(slug, image_tag)
        mark_preserved(slug)
        info(f"to resume from this snapshot: ./cli.py resume {slug}")
        info(
            f"to export for migration:      "
            f"container image save {image_tag} -o {slug}.tar"
        )
        return 0

    if backend == _SMOLMACHINES_BACKEND:
        machine = _agent_machine_name(slug)
        output = _committed_smolmachine_output(slug)
        output.parent.mkdir(parents=True, exist_ok=True)
        pack_create_from_vm(machine, output)
        artifact = _committed_smolmachine_artifact(slug)
        write_committed_image(slug, str(artifact))
        mark_preserved(slug)
        info(f"to resume from this snapshot: ./cli.py resume {slug}")
        info(f"to export for migration:      cp {artifact} {slug}.smolmachine")
        return 0

    if backend:
        die(
            f"commit is only supported for docker, macos-container, and "
            f"smolmachines; "
            f"bottle {slug!r} uses {backend!r}"
        )
    die(f"commit cannot determine the backend for bottle {slug!r}")
    return 1
