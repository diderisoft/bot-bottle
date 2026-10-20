"""The bootable rootfs for an infra VM, resolved from the published manifest.

Firecracker boots a per-plane ext4; every other backend runs the same images as
OCI containers. Both come from one pipeline now (`bot_bottle.publish_images`),
so this only has to answer two questions: which published artifact does this
checkout expect, and how does it become a disk this boot can write to.

Resolution and verification are backend-neutral and live in `image_registry`.
What stays here is the part that is genuinely about running a microVM:
decompressing into a *fresh* rootfs per boot, because the VM mutates its disk
and the cached download must stay pristine for the next one.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from ... import image_inputs, image_registry
from ...contract.hostctl import InfraSettings
from ...infra_image import pinned_infra_artifact
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
    """Whether this host builds the infra rootfs from source instead of pulling
    the published one: when asked to (`infra_build_local`), or when nothing
    published matches this install's code (`infra_image`)."""
    return any(
        pinned_infra_artifact(role_image(r), FORMAT_EXT4, local_build=infra.infra_build_local)
        is None for r in ROLES)


def role_image(role: str) -> str:
    """The published image whose rootfs `role` boots."""
    try:
        return _ROLE_IMAGE[role]
    except KeyError:
        raise KeyError(
            f"{role!r} is not an infra role (known: {', '.join(sorted(_ROLE_IMAGE))})"
        ) from None


def role_artifact(role: str):
    """The ext4 artifact this checkout expects for `role`, on this platform."""
    entries = load_artifacts_manifest()
    return resolve(
        entries, role_image(role), prefer=(FORMAT_EXT4,), plat=current_platform(),
    )


def role_version(role: str, infra: InfraSettings) -> str:
    """What identifies the artifact `role` should be running.

    The `inputs_hash`, not the release `version`. The version is a release number
    shared by every image in a release, so it does not move when code does — and
    this value is what decides whether a running VM may be adopted or must be
    replaced. A marker that cannot change per commit would adopt a VM booted from
    superseded code.

    In the default pull path this is the manifest's stored hash, which names the
    artifact actually fetched. Under `BOT_BOTTLE_INFRA_BUILD=local` the booted
    rootfs is built from the live checkout, not from the committed manifest, so
    the marker is computed from the checkout instead — otherwise editing code and
    rebuilding locally would leave the committed hash unchanged and adopt the
    stale VM (the very staleness this marker exists to prevent)."""
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
    """Expand `role`'s verified rootfs into `dest`, a fresh disk for this boot.

    The cached `.gz` stays pristine: the VM writes to its rootfs, so each boot
    gets its own copy rather than sharing one. Atomic via a `.part` sibling, so
    an interrupted expand cannot be booted."""
    artifact = role_artifact(role)
    gz = image_registry.ensure_ext4(
        artifact, cache_root=host_artifact_cache_dir(infra.artifact_cache))
    tmp = dest.with_suffix(dest.suffix + ".part")
    info(f"expanding {role} rootfs -> {dest}")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, _CHUNK)
    tmp.replace(dest)


def ensure_available(infra: InfraSettings) -> None:
    """Fetch and verify every role's rootfs before any VM boots.

    Up front rather than lazily: booting the orchestrator and then failing to
    fetch the gateway leaves half a control plane running."""
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
