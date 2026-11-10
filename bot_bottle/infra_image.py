"""Pull or build the infra images (gateway + orchestrator).

Pull the release-pinned image only when its recorded `inputs_hash` matches this
install's sources. Building them locally is opt-in (`bot-bottle up --build` or
`BOT_BOTTLE_INFRA_BUILD=local`), never a fallback: without the opt-in, an
install with nothing published to match stops with `InfraImagesUnpublished`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache

from . import image_inputs, image_registry
from .compat import PROTOCOL_VERSION
from .engine import install_version
from .image_registry import RegistryError
from .lib import log
from .lib.log import info
from .published_artifacts import (
    Artifact, FORMAT_OCI, current_platform, load_artifacts_manifest_optional,
)

# How the backends that build with Docker (docker, firecracker) and with
# Apple's `container` (macos-container) describe a local build.
BUILD_NEEDS_DOCKER = "needs Docker"
BUILD_WITH_CONTAINER = "with Apple's `container`"

# How to get an install whose infra images are published: re-running the
# installer installs the latest release tag. One constant, so a move to
# another forge or a package index swaps one line.
RELEASE_INSTALL_HINT = (
    'curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh')


class InfraImagesUnpublished(RegistryError):
    """Nothing published matches this install's infra code and building them
    locally wasn't asked for."""


def unpublished_message(build_note: str = BUILD_NEEDS_DOCKER) -> str:
    """Why bring-up stopped, and the two ways forward."""
    version, sha = install_version()
    label = f"{version} @ {sha}" if sha else version
    return (f"this install ({label}, unreleased) has no published infra images.\n"
            f"    Install a release: {RELEASE_INSTALL_HINT}\n"
            f"    Or build them locally ({build_note}): bot-bottle up --build\n"
            "    (BOT_BOTTLE_INFRA_BUILD=local does the same for every command.)")


def published_infra_artifact(image: str, fmt: str) -> Artifact | None:
    """The published `fmt` artifact of infra `image` for this platform, when
    it was built from this install's code; else None."""
    entries = load_artifacts_manifest_optional()
    entry = entries.get(image) if entries else None
    if entry is None or entry.inputs_hash != _local_inputs_hash(image):
        return None
    return entry.for_format(fmt, current_platform())


def published_infra_matches(fmt: str) -> bool:
    """Whether every infra image has a published `fmt` artifact built from
    this install's code."""
    return all(published_infra_artifact(image, fmt) is not None
               for image in image_inputs.INFRA_ROLES)


def require_infra_images(fmt: str, *, local_build: bool,
                         build_note: str = BUILD_NEEDS_DOCKER) -> None:
    """Raise `InfraImagesUnpublished` unless the infra images can be pulled
    or building them was asked for."""
    if not local_build and not published_infra_matches(fmt):
        raise InfraImagesUnpublished(unpublished_message(build_note))


def pinned_infra_artifact(
    image: str, fmt: str, *, local_build: bool, build_note: str = BUILD_NEEDS_DOCKER,
) -> Artifact | None:
    """The published `fmt` artifact of infra `image` to pull, or None to build
    locally, which only `local_build` asks for. Raises
    `InfraImagesUnpublished` when nothing published matches and building
    wasn't asked for."""
    if local_build:
        return None
    artifact = published_infra_artifact(image, fmt)
    if artifact is None:
        raise InfraImagesUnpublished(unpublished_message(build_note))
    return artifact


@cache
def _local_inputs_hash(image: str) -> str:
    return image_inputs.inputs_hash(image)


def local_build_tag(image: str, local_ref: str) -> str:
    """Tag of a local build of `image` from this checkout: `<repo>:src-<inputs
    hash>`, reusable instead of rebuilding."""
    return f"{_repo(local_ref)}:src-{_local_inputs_hash(image)}"


def note_local_build() -> None:
    """Mark the launch stage for a service image that is being built."""
    log.stage("services", "Rebuilding bot-bottle services")


def pull_pinned_image(
    artifact: Artifact,
    *,
    local_ref: str,
    image_exists: Callable[[str], bool],
    tag_image: Callable[[str, str], None],
    pull_argv: tuple[str, ...],
) -> str:
    """Make the pinned `artifact` available locally and return its tag,
    `<repo>:pin-<digest>`. Pulls by digest after verifying the tag, unless the
    digest is already local. The tag names only this digest, since retagging a
    shared tag like `:latest` can leave a local build answering to it. Raises
    `RegistryError`."""
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


def version_env_vars() -> dict[str, str]:
    """Env vars to inject into an orchestrator container at startup so it can
    report its own version from `GET /version` without needing the full
    host-side package."""
    ver, commit = install_version()
    env: dict[str, str] = {
        "BOT_BOTTLE_VERSION": ver,
        "BOT_BOTTLE_COMMIT": commit,
        "BOT_BOTTLE_PROTOCOL": str(PROTOCOL_VERSION),
    }
    for image in image_inputs.INFRA_ROLES:
        role = image.removeprefix("bot-bottle-").upper()  # GATEWAY or ORCHESTRATOR
        prefix = f"BOT_BOTTLE_IMAGE_{role}"
        try:
            artifact = published_infra_artifact(image, FORMAT_OCI)
            inputs_hash = _local_inputs_hash(image)
        except Exception:  # noqa: BLE001
            continue
        if artifact is not None:
            env[f"{prefix}_SOURCE"] = "published"
            env[f"{prefix}_DIGEST"] = artifact.digest
            env[f"{prefix}_INPUTS_HASH"] = inputs_hash
        else:
            env[f"{prefix}_SOURCE"] = "local"
            env[f"{prefix}_INPUTS_HASH"] = inputs_hash
    return env


__all__ = [
    "BUILD_NEEDS_DOCKER",
    "BUILD_WITH_CONTAINER",
    "InfraImagesUnpublished",
    "published_infra_artifact",
    "published_infra_matches",
    "pinned_infra_artifact",
    "pull_pinned_image",
    "require_infra_images",
    "unpublished_message",
    "version_env_vars",
]
