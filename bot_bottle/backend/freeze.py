"""Freezer — snapshot a running bottle to a resumable artifact.

Follows the same pattern as BottleBackend: a shared base class with
common post-freeze steps (write committed-image path, mark preserved,
print resume hint) and backend-specific subclasses in their respective
backend directories.

Entry points:
  Freezer.commit(agent)      — freeze by ActiveAgent
  Freezer.commit_slug(slug)  — convenience wrapper for cmd_commit
  get_freezer(backend_name)  — factory
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from . import ActiveAgent
from ..bottle_state import mark_preserved, write_committed_image
from ..lib.log import die, info


class CommitCancelled(Exception):
    """Raised by Freezer._freeze when the user declines a confirmation prompt."""


class Freezer(ABC):
    """Freezes a running bottle to a resumable artifact.

    The base class owns the shared post-commit steps:
      - write_committed_image  — records the artifact path in per-bottle state
      - mark_preserved         — prevents teardown from removing the state dir
      - resume hint            — printed to stderr after the snapshot

    Subclasses implement _freeze with the backend-specific snapshot
    operation and optionally override _export_hint for migration hints.
    """

    backend_name: str

    def commit(self, agent: ActiveAgent) -> None:
        """Freeze the bottle for `agent` to a resumable artifact.

        Calls _freeze for the backend-specific snapshot, then writes the
        committed image reference to per-bottle state and marks the bottle
        preserved so the next `bot-bottle resume` boots from the snapshot.

        Raises CommitCancelled if the user declines an interactive
        confirmation prompt (e.g. the macos-container stop prompt).
        """
        image_ref = self._freeze(agent)
        write_committed_image(agent.slug, image_ref)
        mark_preserved(agent.slug)
        info(f"to resume from this snapshot: bot-bottle resume {agent.slug}")
        self._export_hint(agent.slug, image_ref)

    @abstractmethod
    def _freeze(self, agent: ActiveAgent) -> str:
        """Backend-specific snapshot. Returns the image tag or artifact path
        stored by write_committed_image. Raises CommitCancelled if the user
        declines a stop-confirmation prompt."""

    def _export_hint(self, slug: str, image_ref: str) -> None:
        """Optionally print an export-for-migration hint after committing.
        Overridden by backends that provide a meaningful export command."""

    def commit_slug(self, slug: str) -> None:
        """Convenience entry for cmd_commit when only a slug is available."""
        from ..bottle_state import read_metadata
        metadata = read_metadata(slug)
        agent = ActiveAgent(
            backend_name=self.backend_name,
            slug=slug,
            agent_name=metadata.agent_name if metadata else "",
            started_at=metadata.started_at if metadata else "",
            services=(),
        )
        self.commit(agent)


def get_freezer(backend_name: str) -> Freezer:
    """Return the Freezer for the named backend.

    backend_name "" is treated as "docker" for backward compatibility
    with state dirs written before the backend field was added."""
    resolved = backend_name or "docker"
    if resolved == "docker":
        from .docker.freezer import DockerFreezer
        return DockerFreezer()
    if resolved == "macos-container":
        from .macos_container.freezer import MacosContainerFreezer
        return MacosContainerFreezer()
    if resolved == "firecracker":
        from .firecracker.freezer import FirecrackerFreezer
        return FirecrackerFreezer()
    die(
        f"commit is only supported for docker, macos-container, and "
        f"firecracker; backend {backend_name!r} has no freezer"
    )
    raise AssertionError("unreachable")
