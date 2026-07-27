"""External, commit-addressed release bundle metadata."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .release_manifest import ReleaseManifestError, parse_manifest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle_index(
    manifest_data: Any,
    *,
    wheel: Path,
    wheel_url: str,
    workflow_run: str,
    published_at: str,
) -> dict[str, Any]:
    """Build and validate the immutable external index for one source commit."""
    manifest = parse_manifest(manifest_data)
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ReleaseManifestError("release bundle wheel must be one .whl file")
    if not wheel_url.startswith("https://") or not wheel_url.endswith(wheel.name):
        raise ReleaseManifestError(
            "release bundle wheel URL must be HTTPS and end with the wheel filename")
    if not workflow_run.strip() or not published_at.strip():
        raise ReleaseManifestError(
            "release bundle provenance requires workflow_run and published_at")
    return {
        "schema": 1,
        "source_commit": manifest.source_commit,
        "wheel": {
            "filename": wheel.name,
            "url": wheel_url,
            "sha256": sha256_file(wheel),
        },
        "oci": {
            "orchestrator": manifest.orchestrator_image,
            "gateway": manifest.gateway_image,
            **{
                f"agent_{role}": reference
                for role, reference in manifest.agent_images.items()
            },
        },
        "firecracker": {
            "orchestrator": {
                "version": manifest.firecracker_orchestrator.version,
                "sha256": manifest.firecracker_orchestrator.sha256,
            },
            "gateway": {
                "version": manifest.firecracker_gateway.version,
                "sha256": manifest.firecracker_gateway.sha256,
            },
        },
        "provenance": {
            "workflow_run": workflow_run.strip(),
            "published_at": published_at.strip(),
        },
        "qualifications": [],
    }


def parse_bundle_index(data: Any) -> dict[str, Any]:
    """Validate an index before publication or installer consumption."""
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ReleaseManifestError("release bundle index must use schema 1")
    commit = data.get("source_commit")
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        raise ReleaseManifestError(
            "release bundle source_commit must be 40 lowercase hex")
    wheel = data.get("wheel")
    if not isinstance(wheel, dict):
        raise ReleaseManifestError("release bundle wheel must be an object")
    filename, url, digest = (
        wheel.get("filename"), wheel.get("url"), wheel.get("sha256"))
    if not isinstance(filename, str) or not filename.endswith(".whl"):
        raise ReleaseManifestError("release bundle wheel filename must end in .whl")
    if not isinstance(url, str) or not url.startswith("https://") or not url.endswith(filename):
        raise ReleaseManifestError(
            "release bundle wheel URL must be HTTPS and match its filename")
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise ReleaseManifestError(
            "release bundle wheel sha256 must be 64 lowercase hex")
    manifest_data = {
        "schema": 1,
        "source_commit": commit,
        "oci": data.get("oci"),
        "firecracker": data.get("firecracker"),
    }
    parse_manifest(manifest_data)
    provenance = data.get("provenance")
    if not isinstance(provenance, dict) or not all(
        isinstance(provenance.get(key), str) and provenance[key].strip()
        for key in ("workflow_run", "published_at")
    ):
        raise ReleaseManifestError("release bundle provenance is incomplete")
    qualifications = data.get("qualifications")
    if not isinstance(qualifications, list):
        raise ReleaseManifestError("release bundle qualifications must be an array")
    return data


def canonical_bytes(data: Any) -> bytes:
    parse_bundle_index(data)
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


__all__ = [
    "build_bundle_index",
    "canonical_bytes",
    "parse_bundle_index",
    "sha256_file",
]
