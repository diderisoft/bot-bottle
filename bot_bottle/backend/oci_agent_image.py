"""Published agent images for the OCI backends: docker and macos_container
(PRD 0085).

Both run the agent from a local image store, so they resolve it the same way:
pull the release-pinned image, or build the bottle's overlay on it, and give
the result a tag derived from its content (`pin-<digest>` or
`overlay-<key>`). A content tag means two bottles can never race over one
mutable tag, a `--cached-images` launch finds exactly the image it would have
built, and an image that is already local launches without the registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import agent_image, agent_overlay, image_registry
from ..image_registry import RegistryError
from ..lib.log import die, info
from ..published_artifacts import FORMAT_OCI, Artifact, ArtifactsManifestError
from .base import BottlePlan


@dataclass(frozen=True)
class OciRuntime:
    """The image-store operations a backend provides. `pull_argv` is the
    command prefix that pulls a reference; `verify` smoke-tests a fresh image
    (None skips it)."""

    pull_argv: tuple[str, ...]
    image_exists: Callable[[str], bool]
    tag_image: Callable[[str, str], None]
    build_image: Callable[..., None]
    verify: Callable[[str, tuple[str, ...]], None] | None = None


def resolve_published_agent_image(
    plan: BottlePlan,
    runtime: OciRuntime,
    *,
    template: str,
    image: str,
    smoke_test: tuple[str, ...],
) -> str | None:
    """The local tag of the published agent image this bottle runs, pulling or
    building it as needed, or None when nothing is published for it and the
    caller should build its own Dockerfile (a custom `agent_provider.dockerfile`,
    or no manifest yet). An overlay with no published base fails closed rather
    than silently building the default image without it."""
    provider = plan.manifest.bottle.agent_provider
    if provider.builds_custom_image:
        return None
    overlay = provider.overlay
    try:
        pin = agent_image.resolve_agent_pin(template, fmt=FORMAT_OCI)
    except ArtifactsManifestError as e:
        die(str(e))
    if pin is None:
        if not overlay.is_empty():
            raise agent_overlay.no_published_base_error(template)
        return None
    pinned_ref = image_registry.pinned_reference(pin)
    repo = image.rpartition(":")[0] or image
    cached = plan.spec.image_policy == "cached"

    if overlay.is_empty():
        tag = f"{repo}:pin-{pin.digest.removeprefix('sha256:')[:16]}"
        if runtime.image_exists(tag):
            info(f"using published agent image {tag!r}")
            return tag
        if cached:
            _missing_cached(tag)
        _pull(pin, runtime)
        runtime.tag_image(pinned_ref, tag)
        _verify(runtime, tag, smoke_test)
        info(f"using published agent image {tag!r} (pulled {pinned_ref})")
        return tag

    with agent_overlay.staged_overlay(
        pinned_ref, overlay, template=template, source_root=Path(plan.spec.user_cwd),
    ) as staged:
        tag = f"{repo}:overlay-{staged.cache_key}"
        if runtime.image_exists(tag):
            info(f"using overlay agent image {tag!r}")
            return tag
        if cached:
            _missing_cached(tag)
        if not runtime.image_exists(pinned_ref):
            _pull(pin, runtime)
        runtime.build_image(tag, str(staged.context), dockerfile=str(staged.dockerfile))
    _verify(runtime, tag, smoke_test)
    info(f"built overlay agent image {tag!r} on {pinned_ref}")
    return tag


def _pull(pin: Artifact, runtime: OciRuntime) -> None:
    """Verify the pin's tag still points at its digest, then pull by digest.
    Registry failures end the launch with the cause, not a traceback."""
    try:
        resolved = image_registry.resolve(pin)
        image_registry.pull(resolved, argv=runtime.pull_argv)
    except RegistryError as e:
        die(str(e))


def _verify(runtime: OciRuntime, tag: str, smoke_test: tuple[str, ...]) -> None:
    # Smoke-test published and overlaid images too, not just local builds: an
    # image that can't launch the harness should fail here, not at start.
    if runtime.verify is not None:
        runtime.verify(tag, smoke_test)


def _missing_cached(tag: str) -> None:
    die(f"cached agent image {tag!r} not found; run without --cached-images to build it")


__all__ = ["OciRuntime", "resolve_published_agent_image"]
