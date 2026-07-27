"""Immutable infrastructure identities assigned when bot-bottle is packaged."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import resources

_MANIFEST_NAME = "release-manifest.json"
_OCI_RE = re.compile(r"^[^@\s]+@sha256:([0-9a-f]{64})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseManifestError(RuntimeError):
    """The installed package has no usable infrastructure release identity."""


@dataclass(frozen=True)
class FirecrackerArtifact:
    version: str
    sha256: str


@dataclass(frozen=True)
class ReleaseManifest:
    source_commit: str
    orchestrator_image: str
    gateway_image: str
    firecracker_orchestrator: FirecrackerArtifact
    firecracker_gateway: FirecrackerArtifact


def manifest_path() -> Path:
    override = os.environ.get("BOT_BOTTLE_RELEASE_MANIFEST", "").strip()
    return Path(override) if override else Path(__file__).resolve().parent / _MANIFEST_NAME


def _artifact(value: Any, role: str) -> FirecrackerArtifact:
    if not isinstance(value, dict):
        raise ReleaseManifestError(f"release manifest firecracker.{role} must be an object")
    version = value.get("version")
    sha256 = value.get("sha256")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseManifestError(
            f"release manifest firecracker.{role}.version must be non-empty")
    if not isinstance(sha256, str) or _SHA_RE.fullmatch(sha256) is None:
        raise ReleaseManifestError(
            f"release manifest firecracker.{role}.sha256 must be 64 lowercase hex")
    return FirecrackerArtifact(version.strip(), sha256)


def parse_manifest(data: Any) -> ReleaseManifest:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ReleaseManifestError("release manifest must use schema 1")
    commit = data.get("source_commit")
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseManifestError(
            "release manifest source_commit must be 40 lowercase hex")
    oci = data.get("oci")
    if not isinstance(oci, dict):
        raise ReleaseManifestError("release manifest oci must be an object")
    images: dict[str, str] = {}
    for role in ("orchestrator", "gateway"):
        ref = oci.get(role)
        if not isinstance(ref, str) or _OCI_RE.fullmatch(ref) is None:
            raise ReleaseManifestError(
                f"release manifest oci.{role} must be digest-pinned")
        images[role] = ref
    firecracker = data.get("firecracker")
    if not isinstance(firecracker, dict):
        raise ReleaseManifestError("release manifest firecracker must be an object")
    return ReleaseManifest(
        source_commit=commit,
        orchestrator_image=images["orchestrator"],
        gateway_image=images["gateway"],
        firecracker_orchestrator=_artifact(
            firecracker.get("orchestrator"), "orchestrator"),
        firecracker_gateway=_artifact(firecracker.get("gateway"), "gateway"),
    )


def load_manifest() -> ReleaseManifest:
    path = manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(
            f"bot-bottle was packaged without valid infrastructure pins "
            f"({path}): {exc}") from exc
    return parse_manifest(data)


def packaged_manifest() -> ReleaseManifest | None:
    """Release identity, or ``None`` for checkouts/ad-hoc development wheels."""
    if resources.is_source_checkout() and not os.environ.get(
        "BOT_BOTTLE_RELEASE_MANIFEST"
    ):
        return None
    path = manifest_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return load_manifest()  # raise the detailed, fail-closed error
    if isinstance(raw, dict) and raw.get("development") is True:
        return None
    return load_manifest()


def local_build_requested() -> bool:
    return os.environ.get("BOT_BOTTLE_INFRA_BUILD", "").strip().lower() == "local"


def oci_image(role: str, local_default: str) -> tuple[str, bool]:
    """Return ``(reference, should_build_locally)`` for a fixed OCI service."""
    override_name = f"BOT_BOTTLE_{role.upper()}_IMAGE"
    override = os.environ.get(override_name, "").strip()
    manifest = packaged_manifest()
    local = local_build_requested() or manifest is None
    if override:
        if not local and _OCI_RE.fullmatch(override) is None:
            raise ReleaseManifestError(
                f"{override_name} must be digest-pinned outside local build mode")
        return override, local
    if local:
        return local_default, True
    assert manifest is not None
    return (
        manifest.orchestrator_image if role == "orchestrator"
        else manifest.gateway_image,
        False,
    )


__all__ = [
    "FirecrackerArtifact",
    "ReleaseManifest",
    "ReleaseManifestError",
    "load_manifest",
    "local_build_requested",
    "oci_image",
    "packaged_manifest",
    "parse_manifest",
]
