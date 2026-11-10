"""Promotion: copy a tested candidate to a release version, never rebuilding.

OCI images are copied byte for byte through the registry API; rootfs bytes
are downloaded (verified) and published afresh. An entry the candidate reused
from a release, already in the publish registry, is kept as it is.

`publish_images` imports this module at load, so this one imports it back
only inside functions.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import oci_registry
from .published_artifacts import FORMAT_OCI, Artifact, parse_artifacts_manifest
from .publish_reuse import ReusedImage, kept_entry

if TYPE_CHECKING:
    from .publish_images import BuiltImage

def promote(
    manifest: Path, to_version: str, token: str, *, runtime: str = "docker",
) -> list[BuiltImage | ReusedImage]:
    """Copy every artifact in a tested manifest to `to_version`, never
    rebuilding.

    OCI images are copied byte for byte through the registry API into the
    publish registry, so their digests can't change, even from another
    registry. Rootfs bytes are downloaded (verified) and published afresh.
    An entry already in the publish registry is kept (`publish_reuse`)."""
    from . import publish_images as pi
    data = json.loads(manifest.read_text())
    entries = parse_artifacts_manifest(data)
    promoted: list[BuiltImage | ReusedImage] = []
    for name, entry in sorted(entries.items()):
        if (kept := kept_entry(entry, data[name])) is not None:
            promoted.append(kept)
            continue
        out = pi.BuiltImage(name=name, version=to_version, protocol=entry.protocol)
        for artifact in entry.artifacts:
            if artifact.format == FORMAT_OCI:
                digest = _promote_oci(artifact, to_version, runtime=runtime)
            else:
                digest = _promote_ext4(artifact, to_version, token)
            if digest != artifact.digest:
                raise pi.PublishError(
                    f"{name} {artifact.format}/{artifact.platform}: promoted "
                    f"digest {digest} does not match the tested "
                    f"{artifact.digest} — refusing to ship untested bytes"
                )
            is_oci = artifact.format == FORMAT_OCI
            out.artifacts.append(pi.BuiltArtifact(
                fmt=artifact.format, platform=artifact.platform, digest=digest,
                locator=(pi.oci_tag(to_version, artifact.platform) if is_oci
                         else pi.ext4_filename(artifact.platform)),
                # OCI ref from the tested manifest; ext4's namespace is unused.
                namespace=(pi.oci_ref(artifact.image) if is_oci
                           else pi.ext4_package(artifact.image)),
                inputs_hash=entry.inputs_hash,
            ))
        promoted.append(out)
    return promoted


def _promote_oci(
    artifact: Artifact, to_version: str, *, runtime: str = "docker",
) -> str:
    """Copy a published image, by its pinned digest, to the new version's tag
    in the publish registry and return the digest. `runtime` is unused: the
    copy goes through the registry API."""
    del runtime
    from . import image_registry, publish_images as pi

    target = pi.oci_ref(artifact.image)
    tag = pi.oci_tag(to_version, artifact.platform)
    print(f"  promoting {artifact.image} {artifact.platform} -> {target}:{tag}")
    with tempfile.TemporaryDirectory(prefix="bb-promote-") as scratch:
        try:
            source = oci_registry.parse_ref(artifact.url)
            src = oci_registry.Registry(source.host, token=image_registry.token_for(source.host))
            return oci_registry.copy_manifest(
                src, source.repo, artifact.digest, dst=pi._registry(),
                dst_repo=target.split("/", 1)[1], tag=tag, scratch=Path(scratch))
        except oci_registry.OciRegistryError as e:
            raise pi.PublishError(f"promoting {artifact.url}@{artifact.digest} failed: {e}") from e


def _promote_ext4(artifact: Artifact, to_version: str, token: str) -> str:
    """Re-upload a published rootfs, downloaded through the verified path, at
    the new version and return its digest."""
    from . import image_registry, publish_images as pi
    from .cli.settings import settings_from_env
    from .paths import host_artifact_cache_dir

    cache_root = host_artifact_cache_dir(settings_from_env().infra.artifact_cache)
    gz = image_registry.ensure_ext4(artifact, cache_root=cache_root)
    print(f"  promoting {artifact.image} {artifact.platform} rootfs -> {to_version}")
    return pi.publish_ext4(artifact.image, to_version, artifact.platform, gz, token)
