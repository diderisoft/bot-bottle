"""The bootable rootfs for an infra VM, resolved from the published manifest
(resolution and verification are in `image_registry`). Each boot gets a fresh
copy, since the VM writes to its disk.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from ... import image_inputs, image_registry
from ...contract.hostctl import InfraSettings
from ...infra_image import require_infra_images
from ...paths import host_artifact_cache_dir
from ...lib.log import info
from ...published_artifacts import (
    FORMAT_EXT4,
    ArtifactsManifestError,
    current_platform,
    load_artifacts_manifest,
    resolve,
)

ROLES = ("orchestrator", "gateway")

# `image_inputs` maps image -> role; booting needs the inverse.
_ROLE_IMAGE = {role: image for image, role in image_inputs.INFRA_ROLES.items()}

_CHUNK = 1 << 20


def builds_locally(infra: InfraSettings) -> bool:
    """Whether this host builds the infra rootfs from source, which only
    `infra_build_local` (`--build`) asks for. Raises `InfraImagesUnpublished`
    when it wasn't asked for and nothing published matches this install's
    code."""
    require_infra_images(FORMAT_EXT4, local_build=infra.infra_build_local)
    return infra.infra_build_local


def role_image(role: str) -> str:
    """The published image whose rootfs `role` boots."""
    try:
        return _ROLE_IMAGE[role]
    except KeyError:
        raise KeyError(
            f"{role!r} is not an infra role (known: {', '.join(sorted(_ROLE_IMAGE))})"
        ) from None


def role_artifact(role: str):
    """The ext4 artifact this checkout expects for `role` on this platform."""
    entries = load_artifacts_manifest()
    return resolve(
        entries, role_image(role), prefer=(FORMAT_EXT4,), plat=current_platform(),
    )


def role_version(role: str, infra: InfraSettings) -> str:
    """What identifies the artifact `role` should be running: its
    `inputs_hash`, not the release version, which doesn't move per commit.
    Decides whether a running VM may be adopted.

    From the manifest when pulling; computed from the checkout under `--build`
    (`BOT_BOTTLE_INFRA_BUILD=local`), so a local rebuild isn't adopted as
    stale."""
    image = role_image(role)
    if builds_locally(infra):
        return image_inputs.inputs_hash(image)
    entries = load_artifacts_manifest()
    try:
        return entries[image].inputs_hash
    except KeyError:
        raise ArtifactsManifestError(
            f"no published artifact for {image!r} in the manifest"
        ) from None


def materialize_ext4(role: str, dest: Path, infra: InfraSettings) -> None:
    """Expand `role`'s verified rootfs into `dest`, a fresh disk for this boot,
    via a `.part` sibling. The cached `.gz` stays pristine."""
    artifact = role_artifact(role)
    gz = image_registry.ensure_ext4(
        artifact, cache_root=host_artifact_cache_dir(infra.artifact_cache))
    tmp = dest.with_suffix(dest.suffix + ".part")
    info(f"expanding {role} rootfs -> {dest}")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, _CHUNK)
    tmp.replace(dest)


def ensure_available(infra: InfraSettings) -> None:
    """Fetch and verify every role's rootfs before any VM boots, so a failure
    can't leave half a control plane running."""
    for role in ROLES:
        image_registry.ensure_ext4(
            role_artifact(role), cache_root=host_artifact_cache_dir(infra.artifact_cache))


__all__ = [
    "ROLES",
    "builds_locally",
    "ensure_available",
    "materialize_ext4",
    "role_artifact",
    "role_image",
    "role_version",
]
