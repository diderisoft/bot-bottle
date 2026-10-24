"""Resolve built-in agent images from the published manifest.

Returns None when the caller should build locally (no manifest, custom
harness, image not published). An existing pin is digest-verified and fails
closed; it never falls back to a build.
"""

from __future__ import annotations


from . import image_inputs
from .compat import PROTOCOL_VERSION
from .published_artifacts import (
    Artifact,
    ArtifactsManifestError,
    ImageEntry,
    current_platform,
    load_artifacts_manifest_optional,
)


def agent_image_name(harness: str) -> str | None:
    """Published image name for a built-in `harness` (`claude` ->
    `bot-bottle-claude`), or None for a custom harness or an infra role."""
    name = f"bot-bottle-{harness}"
    if name in image_inputs.INFRA_ROLES:  # gateway/orchestrator are not agents
        return None
    return name if name in image_inputs.BUILD_INPUTS else None


def _assert_protocol_compatible(
    entries: dict[str, ImageEntry], agent: ImageEntry,
) -> None:
    """Refuse an agent image built for a different agent <-> infra protocol
    than this host (see `compat`). The protocol, not the release version, must
    match."""
    if agent.protocol != PROTOCOL_VERSION:
        raise ArtifactsManifestError(
            f"agent image {agent.name} speaks protocol {agent.protocol} but this "
            f"bot-bottle speaks protocol {PROTOCOL_VERSION}; refusing to run it"
        )
    for role_image in image_inputs.INFRA_ROLES:
        infra = entries.get(role_image)
        if infra is not None and infra.protocol != agent.protocol:
            raise ArtifactsManifestError(
                f"agent image {agent.name} speaks protocol {agent.protocol} but "
                f"infra image {infra.name} speaks protocol {infra.protocol} in the "
                f"same manifest; refusing to run a mismatched agent/infra pair"
            )


def resolve_agent_pin(
    harness: str, *, fmt: str, plat: str | None = None,
) -> Artifact | None:
    """The pinned `fmt` artifact for `harness` on `plat`, or None to build
    locally. Raises on a protocol mismatch."""
    name = agent_image_name(harness)
    if name is None:
        return None
    entries = load_artifacts_manifest_optional()
    if entries is None:
        return None
    entry = entries.get(name)
    if entry is None:
        return None
    _assert_protocol_compatible(entries, entry)
    return entry.for_format(fmt, plat or current_platform())


__all__ = [
    "agent_image_name",
    "resolve_agent_pin",
]
