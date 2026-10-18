"""Pull or build the infra images (gateway + orchestrator), PRD 0081 / 0085.

Every backend runs the same rule: pull the release-pinned image when it was
built from exactly the code this install has, otherwise build it. Each manifest
entry records the `inputs_hash` of what went into its image, and the install
can compute the same hash from its own sources, so a match means the pulled
image is byte-for-byte what a local build would produce. A mismatch (an install
from `main` past the last release) builds instead of running an image older
than the code that drives it. `BOT_BOTTLE_INFRA_BUILD=local` always builds.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from . import image_inputs, image_registry
from .log import info
from .published_artifacts import Artifact, current_platform, load_artifacts_manifest_optional


def pinned_infra_artifact(image: str, fmt: str) -> Artifact | None:
    """The published `fmt` artifact of infra `image` to pull, or None to build
    it locally: a local build was requested, nothing is published yet, the
    image or format isn't in the manifest, or it was built from other code."""
    if image_registry.local_build_requested():
        return None
    entries = load_artifacts_manifest_optional()
    entry = entries.get(image) if entries else None
    if entry is None:
        return None
    if entry.inputs_hash != _local_inputs_hash(image):
        _note_mismatch(image, entry.version)
        return None
    return entry.for_format(fmt, current_platform())


@cache
def _local_inputs_hash(image: str) -> str:
    return image_inputs.inputs_hash(image)


@cache
def _note_mismatch(image: str, version: str) -> None:
    info(f"published {image} {version} was built from different code than this "
         "install; building it locally")


def pull_pinned_image(
    artifact: Artifact,
    *,
    tag: str,
    image_exists: Callable[[str], bool],
    tag_image: Callable[[str, str], None],
    pull_argv: tuple[str, ...],
) -> None:
    """Make the pinned `artifact` available locally as `tag`: pulled by digest
    (after checking its tag still points there) unless the digest is already
    local, so an install that has it launches offline. Raises `RegistryError`
    when the registry disagrees or can't be reached."""
    pinned_ref = image_registry.pinned_reference(artifact)
    if not image_exists(pinned_ref):
        info(f"pulling published {artifact.image} ({artifact.digest[:19]}…)")
        image_registry.pull(image_registry.resolve(artifact), argv=pull_argv)
    tag_image(pinned_ref, tag)


__all__ = ["pinned_infra_artifact", "pull_pinned_image"]
