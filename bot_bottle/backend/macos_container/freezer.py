"""MacosContainerFreezer — snapshot a macOS container bottle.

Apple Container removes containers when they stop, making stop-then-export
impossible. Instead, commit_container execs into the running container and
streams the root filesystem via tar. The bottle continues running after commit.
"""

from __future__ import annotations

from .. import ActiveAgent
from ..freeze import Freezer
from .util import commit_container
from ...lib.log import info


class MacosContainerFreezer(Freezer):
    """Freezes a macOS-container bottle via exec-tar + image rebuild."""

    backend_name = "macos-container"

    def _freeze(self, agent: ActiveAgent) -> str:
        container = f"bot-bottle-{agent.slug}"
        image_tag = f"bot-bottle-committed-{agent.slug}:latest"
        commit_container(
            container, image_tag, no_cache=self.settings.operation.no_cache,
            dns_override=self.settings.infra.macos_container_dns)
        return image_tag

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(
            f"to export for migration:      "
            f"container image save {image_ref} -o {slug}.tar"
        )
