"""The guest kernel and static dropbear every Firecracker VM needs, fetched
from GHCR by digest when the default cache doesn't have them.

They're published once per architecture (`publish`, run by the
`publish-fc-guest` workflow, through `bot_bottle.publish_fc_guest`) as one OCI artifact, `bot-bottle-fc-guest:<arch>`,
and pinned here by digest, so a host only ever writes the bytes this release
names. A path set with BOT_BOTTLE_FC_KERNEL or BOT_BOTTLE_FC_DROPBEAR is the
operator's own and is never fetched or replaced.
"""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Callable
from pathlib import Path

from ...contract.hostctl import InfraSettings
from ...oci_registry import (
    EMPTY_CONFIG, EMPTY_CONFIG_TYPE, MANIFEST_OCI, OciRegistryError, Registry, _digest_bytes,
    sha256_file,
)
from . import util

REGISTRY = "ghcr.io"
REPO = "diderisoft/bot-bottle-fc-guest"
ARTIFACT_TYPE = "application/vnd.bot-bottle.fc-guest.v1"
KERNEL, DROPBEAR = "vmlinux", "dropbear"
# By `platform.machine()`: each file's sha256, the blob it's published as.
DIGESTS: dict[str, dict[str, str]] = {
    "x86_64": {
        KERNEL: "sha256:27a8310b9a727517e9eb02044524b6ceb77de5728e3491b6974d5c846227ecc8",
        DROPBEAR: "sha256:22831c3855606bb770cf3b6492f988bb2f8a7fa10055c296155e1c0b9f1233bb",
    },
}


class GuestFilesError(RuntimeError):
    """A guest file is missing and can't be fetched."""


def _targets(infra: InfraSettings) -> dict[str, Path]:
    """The files fetched into the default cache: those not set by the operator."""
    targets: dict[str, Path] = {}
    if not infra.fc_kernel:
        targets[KERNEL] = util.kernel_path(infra)
    if not infra.fc_dropbear:
        targets[DROPBEAR] = util.dropbear_path(infra)
    return targets


def missing(infra: InfraSettings) -> list[str]:
    """The fetchable guest files the default cache doesn't have yet."""
    return [name for name, path in _targets(infra).items() if not path.is_file()]


def ensure(infra: InfraSettings, *, say: Callable[[str], None] = lambda _line: None,
           registry: Registry | None = None, machine: str | None = None) -> list[str]:
    """Fetch the guest files the default cache is missing; the names fetched.
    GuestFilesError when one is missing and this architecture has none
    published, or the download fails."""
    need = missing(infra)
    if not need:
        return []
    arch = machine or platform.machine()
    digests = DIGESTS.get(arch)
    if digests is None:
        raise GuestFilesError(
            f"no guest kernel or dropbear is published for {arch}; set BOT_BOTTLE_FC_KERNEL "
            "and BOT_BOTTLE_FC_DROPBEAR to your own (a Firecracker guest kernel and a "
            "static dropbear)")
    registry = registry or Registry(REGISTRY)
    targets = _targets(infra)
    for name in need:
        dest = targets[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        say(f"downloading the Firecracker guest {name} ({REGISTRY}/{REPO})")
        try:
            registry.download_blob(REPO, digests[name], dest)
        except OciRegistryError as e:
            raise GuestFilesError(f"couldn't download the guest {name}: {e}") from e
        os.chmod(dest, 0o555)
    return need


def manifest(files: dict[str, Path]) -> bytes:
    """The OCI artifact manifest holding `files` (name -> path), one layer each."""
    layers = [{"mediaType": "application/octet-stream", "digest": sha256_file(path),
               "size": path.stat().st_size,
               "annotations": {"org.opencontainers.image.title": name}}
              for name, path in sorted(files.items())]
    body = {"schemaVersion": 2, "mediaType": MANIFEST_OCI, "artifactType": ARTIFACT_TYPE,
            "config": {"mediaType": EMPTY_CONFIG_TYPE, "digest": _digest_bytes(EMPTY_CONFIG),
                       "size": len(EMPTY_CONFIG)},
            "layers": layers}
    return json.dumps(body, indent=2, sort_keys=True).encode()


def publish(registry: Registry, arch: str, files: dict[str, Path]) -> dict[str, str]:
    """Push `files` as `REPO:<arch>`; each file's digest, to pin in DIGESTS."""
    for path in files.values():
        registry.upload_blob(REPO, sha256_file(path), path)
    registry.upload_blob(REPO, _digest_bytes(EMPTY_CONFIG), EMPTY_CONFIG)
    registry.put_manifest(REPO, arch, manifest(files), MANIFEST_OCI)
    return {name: sha256_file(path) for name, path in files.items()}


__all__ = ["DIGESTS", "DROPBEAR", "GuestFilesError", "KERNEL", "REGISTRY", "REPO", "ensure",
           "manifest", "missing", "publish"]
