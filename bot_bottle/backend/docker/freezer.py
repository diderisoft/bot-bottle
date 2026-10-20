"""DockerFreezer — snapshot a Docker bottle via `docker commit`."""

from __future__ import annotations

from .. import ActiveAgent
from ..freeze import Freezer
from .util import commit_container
from ...lib.log import info


class DockerFreezer(Freezer):
    """Freezes a Docker bottle by running `docker commit`."""

    backend_name = "docker"

    def _freeze(self, agent: ActiveAgent) -> str:
        container = f"bot-bottle-{agent.slug}"
        image_tag = f"bot-bottle-committed-{agent.slug}:latest"
        commit_container(container, image_tag)
        return image_tag

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      docker save {image_ref} -o {slug}.tar")
