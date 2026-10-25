"""Cached agent and infra images on the Docker and macOS (Apple container)
backends, for `bot-bottle prune` (PRD 0088).

Prunable: bot-bottle's own repositories' `pin-<digest>` (published agent
bases), `overlay-<key>` (overlay builds), registry digest refs a pull left,
and `src-<hash>` infra builds. Kept: the current pins' tags and digests,
anything a container uses, and the newest `src-` build per repository.
`latest` tags and frozen bottles (`committed-*`) are never listed.

Removing a tag frees the image only once no other tag shares it, so sizes are
upper bounds.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..agent_image import agent_image_name, resolve_agent_pin
from ..agent_provider import BUILTIN_HARNESSES
from ..published_artifacts import FORMAT_OCI
from .cache_entries import CacheEntry
from .host_resources import IMAGE_PREFIX


@dataclass(frozen=True)
class LocalImage:
    """One local image ref, as a runtime lists it."""

    ref: str
    size: int
    created: float
    in_use: bool


def _split(ref: str) -> tuple[str, str]:
    """(repository basename, tag or `@digest`) of a ref."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo.rsplit("/", 1)[-1], "@" + digest
    repo, _, tag = ref.rpartition(":")
    if "/" in tag:  # a registry port, not a tag
        repo, tag = ref, ""
    return repo.rsplit("/", 1)[-1], tag


def _pins() -> tuple[set[str], set[str]]:
    """The current pins' local tags (`<repo>:pin-<digest16>`) and digests."""
    tags, digests = set(), set()
    for harness in BUILTIN_HARNESSES:
        with contextlib.suppress(Exception):  # no manifest, or not pinned here
            pin = resolve_agent_pin(harness, fmt=FORMAT_OCI)
            name = agent_image_name(harness)
            if pin is not None and name is not None:
                tags.add(f"{name}:pin-{pin.digest.removeprefix('sha256:')[:16]}")
                digests.add(pin.digest)
    return tags, digests


def entries_for(images: Iterable[LocalImage],
                remove: Callable[[str], bool]) -> list[CacheEntry]:
    """The prunable images among `images`, with why any of them stays."""
    pin_tags, pin_digests = _pins()
    found: list[tuple[LocalImage, str]] = []
    for image in images:
        repo, tag = _split(image.ref)
        if not repo.startswith(IMAGE_PREFIX) or repo.startswith(IMAGE_PREFIX + "committed-"):
            continue
        if tag.startswith(("pin-", "overlay-", "src-", "@sha256:")):
            found.append((image, f"{repo}:{tag}" if not tag.startswith("@") else repo + tag))
    newest_src: dict[str, LocalImage] = {}
    for image, _ in found:
        repo, tag = _split(image.ref)
        if tag.startswith("src-") and (repo not in newest_src
                                       or image.created > newest_src[repo].created):
            newest_src[repo] = image
    entries = []
    for image, local in found:
        repo, tag = _split(image.ref)
        keep = None
        if image.in_use:
            keep = "in use by a container"
        elif local in pin_tags or tag.removeprefix("@") in pin_digests:
            keep = "a current pin"
        elif newest_src.get(repo) is image:
            keep = f"the newest {repo} build"
        # Pins and pulled digests are releases; overlays and src- are builds.
        entries.append(CacheEntry(f"image {image.ref}", image.size, image.created, keep,
                                  lambda r=image.ref: remove(r),
                                  build=tag.startswith(("overlay-", "src-"))))
    return entries


__all__ = ["LocalImage", "entries_for"]
