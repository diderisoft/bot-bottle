"""`inputs_hash` — a content hash of everything baked into a published image.

Computable from a checkout before anything is built, so a host can ask whether a
published artifact came from its source, and so an image's formats can be shown
interchangeable (same inputs in, either artifact out).

Architecture-independent: the pinned bases are multi-arch index digests, so one
set of inputs describes both arches and `--platform` selects at build time.
Architecture is carried by the manifest's platform keys instead.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import resources

# Bumped when the *meaning* of the hash changes (inputs added or removed, or the
# framing bytes reordered), forcing a republish rather than letting an old
# artifact answer to a new hash.
_SCHEMA = "image-inputs-v1"

# Per-image build inputs, relative to the build root. The `bot_bottle` package
# is added separately for images that bake it (see `_BAKES_PACKAGE`).
BUILD_INPUTS: dict[str, tuple[str, ...]] = {
    "bot-bottle-gateway": (
        "image-build-args.json",
        "Dockerfile.gateway",
        "requirements.gateway.lock",
    ),
    "bot-bottle-orchestrator": (
        "image-build-args.json",
        "Dockerfile.orchestrator",
        "Dockerfile.orchestrator.fc",
        "requirements.orchestrator.lock",
    ),
    "bot-bottle-claude": (
        "image-build-args.json",
        "bot_bottle/contrib/claude/Dockerfile",
        "bot_bottle/contrib/claude/package.json",
        "bot_bottle/contrib/claude/package-lock.json",
    ),
    "bot-bottle-codex": (
        "image-build-args.json",
        "bot_bottle/contrib/codex/Dockerfile",
        "bot_bottle/contrib/codex/codex-package_SHA256SUMS",
    ),
    "bot-bottle-pi": (
        "image-build-args.json",
        "bot_bottle/contrib/pi/Dockerfile",
        "bot_bottle/contrib/pi/package.json",
        "bot_bottle/contrib/pi/package-lock.json",
    ),
}

# Images that bake the shipped `bot_bottle` package (the orchestrator COPYs it,
# the gateway pip-installs it), so a change anywhere under the package must bump
# their hash. Agent images carry a harness, not our package.
_BAKES_PACKAGE = frozenset({"bot-bottle-gateway", "bot-bottle-orchestrator"})

# The Dockerfile each image builds from, named explicitly rather than positioned
# inside BUILD_INPUTS: the input list is a hashing concern and its order must
# stay free to change without silently repointing a build.
DOCKERFILES: dict[str, str] = {
    "bot-bottle-gateway": "Dockerfile.gateway",
    "bot-bottle-orchestrator": "Dockerfile.orchestrator",
    "bot-bottle-claude": "bot_bottle/contrib/claude/Dockerfile",
    "bot-bottle-codex": "bot_bottle/contrib/codex/Dockerfile",
    "bot-bottle-pi": "bot_bottle/contrib/pi/Dockerfile",
}

# Images whose rootfs boots with a role-specific PID 1 rather than the default
# agent init. Maps to the firecracker infra role name.
INFRA_ROLES: dict[str, str] = {
    "bot-bottle-gateway": "gateway",
    "bot-bottle-orchestrator": "orchestrator",
}

# Build inputs are a property of (image, format), not of an image. The
# orchestrator's OCI image is the lean control plane; its bootable rootfs is the
# `.fc` variant, `FROM` that image plus buildah for in-VM agent builds. Only
# images that differ appear here.
ROOTFS_DOCKERFILES: dict[str, str] = {
    "bot-bottle-orchestrator": "Dockerfile.orchestrator.fc",
}

# The build arg a rootfs Dockerfile takes its base image through. These
# deliberately have no default — the `FROM` would otherwise silently resolve a
# stale `:latest` instead of the image just built — so the value has to be
# supplied, and as a digest-pinned local ref rather than a tag.
ROOTFS_BASE_ARG: dict[str, str] = {
    "bot-bottle-orchestrator": "ORCHESTRATOR_BASE_IMAGE",
}

# Free space to leave in a rootfs beyond its contents. The orchestrator builds
# agent images inside itself, so it needs far more than anything else; the
# default is what every other image gets.
DEFAULT_ROOTFS_SLACK_MIB = 1024
ROOTFS_SLACK_MIB: dict[str, int] = {
    "bot-bottle-orchestrator": 8192,
}


def rootfs_dockerfile(image: str) -> str:
    """The Dockerfile whose image becomes `image`'s bootable rootfs.

    Usually the same one the OCI image is built from. When it differs, it is
    `FROM` the OCI image, so that one must be built first."""
    return ROOTFS_DOCKERFILES.get(image, DOCKERFILES[image])


def rootfs_slack_mib(image: str) -> int:
    """Free space to leave in `image`'s rootfs."""
    return ROOTFS_SLACK_MIB.get(image, DEFAULT_ROOTFS_SLACK_MIB)

IMAGES: tuple[str, ...] = tuple(BUILD_INPUTS)


class UnknownImageError(KeyError):
    """No build-input list is registered for this image name."""


def _hash_package(h: "hashlib._Hash", repo_root: Path) -> None:
    """Fold every regular file under `bot_bottle/` into `h`.

    All files, not just `*.py` — the package is baked in wholesale, shell
    scripts and data included. `__pycache__`/`.pyc` are excluded as build
    artifacts that are never copied."""
    pkg = repo_root / "bot_bottle"
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())


def inputs_hash(image: str, *, repo_root: Path | None = None) -> str:
    """The 16-hex content hash of everything baked into `image`.

    Deterministic across publish and launch hosts on the same checkout, and
    identical on every architecture."""
    try:
        inputs = BUILD_INPUTS[image]
    except KeyError:
        raise UnknownImageError(
            f"{image!r} is not a published image "
            f"(known: {', '.join(sorted(BUILD_INPUTS))})"
        ) from None
    if repo_root is None:
        repo_root = resources.build_root()

    h = hashlib.sha256()
    # Frame every field so a value can never be confused for a boundary.
    h.update(f"schema={_SCHEMA}\nimage={image}\n".encode())
    if image in _BAKES_PACKAGE:
        _hash_package(h, repo_root)
    for name in inputs:
        path = repo_root / name
        h.update(name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    h.update(b"pyproject.toml\0")
    h.update((repo_root / "pyproject.toml").read_bytes())
    return h.hexdigest()[:16]


__all__ = [
    "BUILD_INPUTS",
    "DOCKERFILES",
    "IMAGES",
    "INFRA_ROLES",
    "UnknownImageError",
    "inputs_hash",
]
