"""Snapshot a running bottle to a resumable artifact. The shared `Freezer`
records the artifact, marks the state preserved and prints the resume hint;
backend subclasses implement `_freeze`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from . import ActiveAgent
from ..contract.hostctl import Settings
from ..bottle_state import mark_preserved, write_committed_image
from ..lib.log import die, info


class CommitCancelled(Exception):
    """The user declined a confirmation prompt."""


class Freezer(ABC):
    """Freezes a running bottle to a resumable artifact. Subclasses implement
    `_freeze` and may override `_export_hint`.
    """

    backend_name: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def commit(self, agent: ActiveAgent) -> None:
        """Freeze `agent`'s bottle, record the artifact and mark the bottle
        preserved so `resume` boots from it. Raises CommitCancelled if the user
        declines.
        """
        image_ref = self._freeze(agent)
        write_committed_image(agent.slug, image_ref)
        mark_preserved(agent.slug)
        info(f"to resume from this snapshot: bot-bottle resume {agent.slug}")
        self._export_hint(agent.slug, image_ref)

    @abstractmethod
    def _freeze(self, agent: ActiveAgent) -> str:
        """Backend-specific snapshot; returns the image tag or artifact path.
        Raises CommitCancelled if the user declines."""

    def _export_hint(self, slug: str, image_ref: str) -> None:
        """Print an export-for-migration hint after committing, if the backend
        has one."""

    def commit_slug(self, slug: str) -> None:
        """`commit` by slug."""
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


def get_freezer(settings: Settings, backend_name: str) -> Freezer:
    """The Freezer for `backend_name` ("" means docker, for older state dirs)."""
    resolved = backend_name or "docker"
    if resolved == "docker":
        from .docker.freezer import DockerFreezer
        return DockerFreezer(settings)
    if resolved == "macos-container":
        from .macos_container.freezer import MacosContainerFreezer
        return MacosContainerFreezer(settings)
    if resolved == "firecracker":
        from .firecracker.freezer import FirecrackerFreezer
        return FirecrackerFreezer(settings)
    die(
        f"commit is only supported for docker, macos-container, and "
        f"firecracker; backend {backend_name!r} has no freezer"
    )
    raise AssertionError("unreachable")
