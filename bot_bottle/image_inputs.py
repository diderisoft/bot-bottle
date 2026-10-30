"""`inputs_hash`: a content hash of everything baked into a published image,
computable from a checkout before building. Architecture-independent (bases are
multi-arch index digests).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import resources

# Bump when the hash's meaning changes, forcing a republish.
# v2: pyproject.toml's `[project] version` no longer counts (see
# `_versionless_pyproject`).
_SCHEMA = "image-inputs-v2"

_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_VERSION_KEY = re.compile(r"^(\s*version\s*=).*$")

# Per-image build inputs, relative to the build root; package paths are in
# `PACKAGE_PATHS`.
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

# The `bot_bottle` slice each infra image bakes: contract, lib and its own
# service. Must match the Dockerfile `COPY` lines (a unit test checks).
_SHARED_PACKAGE_PATHS = (
    "bot_bottle/__init__.py",
    "bot_bottle/contract",
    "bot_bottle/lib",
)
PACKAGE_PATHS: dict[str, tuple[str, ...]] = {
    "bot-bottle-gateway": (*_SHARED_PACKAGE_PATHS, "bot_bottle/gateway"),
    "bot-bottle-orchestrator": (*_SHARED_PACKAGE_PATHS, "bot_bottle/orchestrator"),
}

# Each image's Dockerfile, named explicitly so reordering BUILD_INPUTS can't
# repoint a build.
DOCKERFILES: dict[str, str] = {
    "bot-bottle-gateway": "Dockerfile.gateway",
    "bot-bottle-orchestrator": "Dockerfile.orchestrator",
    "bot-bottle-claude": "bot_bottle/contrib/claude/Dockerfile",
    "bot-bottle-codex": "bot_bottle/contrib/codex/Dockerfile",
    "bot-bottle-pi": "bot_bottle/contrib/pi/Dockerfile",
}

# Images whose rootfs boots a role-specific PID 1; maps to the firecracker
# infra role.
INFRA_ROLES: dict[str, str] = {
    "bot-bottle-gateway": "gateway",
    "bot-bottle-orchestrator": "orchestrator",
}

# Rootfs Dockerfiles that differ from the OCI one (each is `FROM` that image).
ROOTFS_DOCKERFILES: dict[str, str] = {
    "bot-bottle-orchestrator": "Dockerfile.orchestrator.fc",
}

# Build arg a rootfs Dockerfile takes its base through. No default, so it must
# be supplied as a digest-pinned ref rather than resolving a stale `:latest`.
ROOTFS_BASE_ARG: dict[str, str] = {
    "bot-bottle-orchestrator": "ORCHESTRATOR_BASE_IMAGE",
}

# Free rootfs space beyond its contents. The orchestrator builds agent images
# inside itself, so it needs more.
DEFAULT_ROOTFS_SLACK_MIB = 1024
ROOTFS_SLACK_MIB: dict[str, int] = {
    "bot-bottle-orchestrator": 8192,
}


def rootfs_dockerfile(image: str) -> str:
    """The Dockerfile whose image becomes `image`'s rootfs. When it differs
    from the OCI one it is `FROM` that image, which must be built first."""
    return ROOTFS_DOCKERFILES.get(image, DOCKERFILES[image])


def rootfs_slack_mib(image: str) -> int:
    """Free space to leave in `image`'s rootfs."""
    return ROOTFS_SLACK_MIB.get(image, DEFAULT_ROOTFS_SLACK_MIB)

IMAGES: tuple[str, ...] = tuple(BUILD_INPUTS)


class UnknownImageError(KeyError):
    """No build-input list is registered for this image name."""


def package_files(image: str, repo_root: Path) -> list[Path]:
    """Every regular file under `image`'s `PACKAGE_PATHS`, sorted, excluding
    `__pycache__`/`.pyc`."""
    files: set[Path] = set()
    for name in PACKAGE_PATHS.get(image, ()):
        root = repo_root / name
        candidates = [root] if root.is_file() else root.rglob("*")
        files.update(
            path for path in candidates
            if path.is_file()
            and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    return sorted(files)


def _hash_package(h: "hashlib._Hash", image: str, repo_root: Path) -> None:
    """Fold the package files `image` bakes into `h`."""
    for path in package_files(image, repo_root):
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())


def _versionless_pyproject(data: bytes) -> bytes:
    """`pyproject.toml` with its `[project] version` value blanked.

    A release publishes and tests images from one commit, then bumps the
    version in the release commit it tags. If the version counted, a release's
    pins could never match the tagged code. No image reads the version: the
    gateway pip-installs the package, so its dist-info carries the tested
    commit's version string, but nothing compares it.
    """
    lines = data.decode("utf-8").splitlines(keepends=True)
    table = ""
    for i, line in enumerate(lines):
        header = _TABLE_HEADER.match(line)
        if header:
            table = header.group(1)
        elif table == "project":
            body = line.rstrip("\r\n")
            match = _VERSION_KEY.match(body)
            if match:
                lines[i] = match.group(1) + ' ""' + line[len(body):]
    return "".join(lines).encode("utf-8")


def inputs_hash(image: str, *, repo_root: Path | None = None) -> str:
    """16-hex content hash of everything baked into `image`, identical across
    hosts and architectures for one checkout."""
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
    # Frame every field so a value can't be mistaken for a boundary.
    h.update(f"schema={_SCHEMA}\nimage={image}\n".encode())
    _hash_package(h, image, repo_root)
    for name in inputs:
        path = repo_root / name
        h.update(name.encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    h.update(b"pyproject.toml\0")
    h.update(_versionless_pyproject((repo_root / "pyproject.toml").read_bytes()))
    return h.hexdigest()[:16]


__all__ = [
    "BUILD_INPUTS",
    "DOCKERFILES",
    "IMAGES",
    "INFRA_ROLES",
    "PACKAGE_PATHS",
    "UnknownImageError",
    "inputs_hash",
    "package_files",
]
