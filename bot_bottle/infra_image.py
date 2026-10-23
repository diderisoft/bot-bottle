"""Pull or build the infra images (gateway + orchestrator).

Every backend runs the same rule: pull the release-pinned image when it was
built from exactly the code this install has, otherwise build it. Each manifest
entry records the `inputs_hash` of what went into its image, and the install
can compute the same hash from its own sources, so a match means the pulled
image is byte-for-byte what a local build would produce. A mismatch (an install
from `main` past the last release) builds instead of running an image older
than the code that drives it. `local_build` (`BOT_BOTTLE_INFRA_BUILD=local`)
always builds.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from . import image_inputs, image_registry
from .lib import log
from .lib.log import info
from .published_artifacts import Artifact, current_platform, load_artifacts_manifest_optional


def pinned_infra_artifact(image: str, fmt: str, *, local_build: bool) -> Artifact | None:
    """The published `fmt` artifact of infra `image` to pull, or None to build
    it locally: `local_build` was requested, nothing is published yet, the
    image or format isn't in the manifest, or it was built from other code."""
    if local_build:
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


def local_build_tag(image: str, local_ref: str) -> str:
    """The tag a local build of `image` from this checkout carries:
    `<repo>:src-<inputs hash>`. An image with it was built from exactly
    these inputs, so a launch can use it instead of building again."""
    return f"{_repo(local_ref)}:src-{_local_inputs_hash(image)}"


def note_local_build() -> None:
    """Mark the launch stage for a service image that's actually being built."""
    log.stage("services", "Rebuilding bot-bottle services")


def pull_pinned_image(
    artifact: Artifact,
    *,
    local_ref: str,
    image_exists: Callable[[str], bool],
    tag_image: Callable[[str, str], None],
    pull_argv: tuple[str, ...],
) -> str:
    """Make the pinned `artifact` available locally and return the tag to run
    in place of `local_ref`: `<its repo>:pin-<digest>`, pulled by digest (after
    checking its tag still points there) unless the digest is already local,
    so an install that has it launches offline. The tag names only this
    digest: retagging a shared one like `:latest` can leave a local build
    answering to it (Apple Container keeps the built image's literal name, and
    its `image tag` normalizes the new one). Raises `RegistryError` when the
    registry disagrees or can't be reached."""
    tag = f"{_repo(local_ref)}:pin-{artifact.digest.removeprefix('sha256:')[:16]}"
    if image_exists(tag):
        return tag
    pinned_ref = image_registry.pinned_reference(artifact)
    if not image_exists(pinned_ref):
        info(f"pulling published {artifact.image} ({artifact.digest[:19]}…)")
        image_registry.pull(image_registry.resolve(artifact), argv=pull_argv)
    tag_image(pinned_ref, tag)
    return tag


def _repo(ref: str) -> str:
    """`ref` without its tag (`host:5000/img:latest` -> `host:5000/img`)."""
    name, _, tag = ref.rpartition(":")
    return name if name and "/" not in tag else ref


__all__ = ["pinned_infra_artifact", "pull_pinned_image"]
