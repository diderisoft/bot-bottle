"""Resolve agent images from the published manifest (PRD 0085).

The consumer side of agent images, mirroring `infra_rootfs` for infra: given a
built-in provider template, find the release-pinned artifact for this platform in
the manifest baked into the install, and materialize it (OCI pull for
docker/macos, ext4 fetch for firecracker).

Resolution returns **None** whenever the caller should build locally instead — no
manifest yet (before the first release), a custom template with no published
default, or an image absent from the manifest. A pin that *does* exist is
verified against its digest and **fails closed** (raises) on tamper or an
unreachable registry; it never silently falls back to a build.
"""

from __future__ import annotations


from . import image_inputs, image_registry
from .compat import PROTOCOL_VERSION
from .published_artifacts import (
    Artifact,
    ArtifactsManifestError,
    FORMAT_OCI,
    ImageEntry,
    current_platform,
    load_artifacts_manifest_optional,
)


def agent_image_name(template: str) -> str | None:
    """The published image name for a built-in agent `template`
    (`claude` -> `bot-bottle-claude`), or None for a custom template or an infra
    role — neither has a published agent default, so the caller builds."""
    name = f"bot-bottle-{template}"
    if name in image_inputs.INFRA_ROLES:  # gateway/orchestrator are not agents
        return None
    return name if name in image_inputs.BUILD_INPUTS else None


def _assert_protocol_compatible(
    entries: dict[str, ImageEntry], agent: ImageEntry,
) -> None:
    """Refuse an agent image built for a different agent <-> infra protocol than
    this host and the infra images in the same manifest (see `compat`).

    The protocol, not the release version, is what has to agree: releases move
    for unrelated reasons (republishing one image, a harness bump), the protocol
    only when the contract an agent boots against changes. A mismatch is an
    incompatibility, not a warning, so fail closed rather than boot an agent its
    infra cannot drive."""
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
    template: str, *, prefer: tuple[str, ...], plat: str | None = None,
) -> Artifact | None:
    """The pinned artifact for `template` on `plat`, or None when the consumer
    should build locally instead: no manifest yet, a custom template, the image
    is absent from the manifest, or it has no artifact in `prefer` for `plat`.

    A pin that *does* exist but was built for a different agent <-> infra protocol
    raises (fail closed) — see `_assert_protocol_compatible`."""
    name = agent_image_name(template)
    if name is None:
        return None
    entries = load_artifacts_manifest_optional()
    if entries is None:
        return None
    entry = entries.get(name)
    if entry is None:
        return None
    _assert_protocol_compatible(entries, entry)
    plat = plat or current_platform()
    for fmt in prefer:
        artifact = entry.for_format(fmt, plat)
        if artifact is not None:
            return artifact
    return None


def ensure_agent_oci(template: str, *, runtime: str = "docker") -> str | None:
    """Pull + verify the pinned OCI agent image for `template`, returning the
    local digest-pinned ref to run, or None when nothing is pinned (build).

    A pin that exists but fails verification/pull raises — the pull path never
    falls back to a build (that is the caller's explicit choice)."""
    artifact = resolve_agent_pin(template, prefer=(FORMAT_OCI,))
    if artifact is None:
        return None
    resolved = image_registry.resolve(artifact)
    image_registry.pull(resolved, runtime=runtime)
    return resolved.pinned_ref


__all__ = [
    "agent_image_name",
    "resolve_agent_pin",
    "ensure_agent_oci",
]
