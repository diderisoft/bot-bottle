"""MacosContainerFreezer — snapshot a macOS container bottle.

Apple's `container export` requires the container to be stopped first.
When the container is running the freezer prompts the user to confirm
the stop before proceeding."""

from __future__ import annotations

import sys

from .. import ActiveAgent
from ..freeze import CommitCancelled, Freezer
from .util import commit_container, container_is_running, stop_container
from ...log import info


class MacosContainerFreezer(Freezer):
    """Freezes a macOS-container bottle via `container export` + image rebuild."""

    backend_name = "macos-container"

    def _freeze(self, agent: ActiveAgent) -> str:
        container = f"bot-bottle-{agent.slug}"
        image_tag = f"bot-bottle-committed-{agent.slug}:latest"
        if container_is_running(container):
            sys.stderr.write(
                f"bot-bottle: bottle {agent.slug!r} is running; "
                "commit will stop it. Continue? [y/N] "
            )
            sys.stderr.flush()
            reply = _read_tty_line().strip().lower()
            if reply not in ("y", "yes"):
                raise CommitCancelled
            stop_container(container)
        commit_container(container, image_tag)
        return image_tag

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(
            f"to export for migration:      "
            f"container image save {image_ref} -o {slug}.tar"
        )


def _read_tty_line() -> str:
    """Read one line from /dev/tty, falling back to stdin."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")
