"""Reuse in a publish: skip an image whose inputs are already published.

An image the reuse manifest (`--reuse-from`, by default the committed one)
already publishes from this tree's `inputs_hash` and protocol, in every
requested format and platform and still served by digest, is neither built
nor uploaded: its entry is copied unchanged. Promotion keeps such an entry
when it already lives in the publish registry.

`publish_images` imports this module at load, so this one imports it back
only inside functions.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import image_inputs, oci_registry
from .compat import PROTOCOL_VERSION
from .published_artifacts import (
    Artifact,
    ArtifactsManifestError,
    FORMAT_EXT4,
    FORMAT_OCI,
    ImageEntry,
    artifacts_manifest_path,
    parse_artifacts_manifest,
)

# Beside a promoted manifest: the release version. Reused entries keep their
# own, so the manifest's entries can't say it.
RELEASE_VERSION_NAME = "release-version"


@dataclass
class ReusedImage:
    """An image already published from these inputs. `entry` is its manifest
    entry, written back byte for byte: its URLs keep naming the version that
    published it."""

    name: str
    entry: dict[str, object]

    @property
    def version(self) -> str:
        return str(self.entry.get("version", ""))

    @property
    def inputs_hash(self) -> str:
        return str(self.entry.get("inputs_hash", ""))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """The publish CLI's reuse flags."""
    parser.add_argument(
        "--reuse-from", type=Path, default=None, metavar="MANIFEST",
        help="skip building an image MANIFEST already publishes from this tree's "
             "inputs_hash, in every requested format and platform, and copy its "
             "entry unchanged. Default: the repo-root published-artifacts.json "
             "(the last release), when present.",
    )
    parser.add_argument(
        "--no-reuse", action="store_true",
        help="build and publish every image, even ones whose inputs are "
             "already published. --force alone does not imply this.",
    )


def _host_token(host: str) -> str:
    """The token to read from `host`: the publish token on the publish
    registry, the scoped artifact token elsewhere, else none (public)."""
    from . import publish_images as pi
    from . import image_registry
    base, _, token = pi._config()
    if host.lower() == base.split("://", 1)[-1].lower():
        return token
    return image_registry.token_for(host)


def artifact_present(artifact: Artifact) -> bool:
    """Whether `artifact`'s registry still serves it by its pinned digest: the
    OCI manifest, the rootfs layer, or the generic-package file. Raises
    `PublishError` when the registry can't be asked."""
    from . import publish_images as pi
    if "://" in artifact.url:
        host = urllib.parse.urlsplit(artifact.url).netloc
        status = pi._head(artifact.url, _host_token(host))
        if status == 0:
            raise pi.PublishError(f"checking {artifact.url} failed: registry unreachable")
        return status == 200
    try:
        ref = oci_registry.parse_ref(artifact.url)
        base, _, _ = pi._config()
        if ref.host.lower() == base.split("://", 1)[-1].lower():
            registry = pi._registry()
        else:
            registry = oci_registry.Registry(ref.host, token=_host_token(ref.host))
        if artifact.format == FORMAT_OCI:
            return registry.manifest_exists(ref.repo, artifact.digest)
        return registry.blob_exists(ref.repo, artifact.digest)
    except oci_registry.OciRegistryError as e:
        raise pi.PublishError(f"checking {artifact.url}@{artifact.digest} failed: {e}") from e


def required_artifacts(platforms: list[str], *, no_ext4: bool = False) -> set[tuple[str, str]]:
    """The `(format, platform)` pairs a publish produces per image."""
    formats = [FORMAT_OCI] if no_ext4 else [FORMAT_OCI, FORMAT_EXT4]
    return {(fmt, plat) for fmt in formats for plat in platforms}


def rebuild_reason(
    entry: ImageEntry | None, local_hash: str, required: set[tuple[str, str]],
) -> str | None:
    """Why `entry` can't stand in for a build from these inputs, or None when
    it can. Presence in the registry is checked separately."""
    if entry is None:
        return "not in the reuse manifest"
    if not entry.inputs_hash or entry.inputs_hash != local_hash:
        return f"inputs {local_hash} differ from {entry.version}'s {entry.inputs_hash}"
    if entry.protocol != PROTOCOL_VERSION:
        return (f"{entry.version} speaks protocol {entry.protocol}, "
                f"this tree {PROTOCOL_VERSION}")
    have = {(a.format, a.platform) for a in entry.artifacts}
    missing = sorted(required - have)
    if missing:
        return f"{entry.version} lacks " + ", ".join(f"{f} {p}" for f, p in missing)
    return None


def plan_reuse(
    images: list[str],
    platforms: list[str],
    source: Path,
    *,
    repo_root: Path,
    no_ext4: bool = False,
) -> dict[str, ReusedImage]:
    """The images in `source` that need no build: same `inputs_hash` and
    protocol as this tree, every required format and platform present, and
    every artifact still in its registry. Logs each decision."""
    from . import publish_images as pi
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        entries = parse_artifacts_manifest(data)
    except (OSError, json.JSONDecodeError, ArtifactsManifestError) as e:
        raise pi.PublishError(f"reuse manifest {source}: {e}") from e
    required = required_artifacts(platforms, no_ext4=no_ext4)

    reused: dict[str, ReusedImage] = {}
    for name in images:
        entry = entries.get(name)
        local = image_inputs.inputs_hash(name, repo_root=repo_root) if entry else ""
        reason = rebuild_reason(entry, local, required)
        if reason is None and entry is not None:
            try:
                gone = [a for a in entry.artifacts if not artifact_present(a)]
            except pi.PublishError as e:
                gone, reason = [], f"could not confirm it is still published ({e})"
            if gone:
                reason = "gone from its registry: " + ", ".join(
                    f"{a.url}@{a.digest}" for a in gone)
        if reason is not None or entry is None:
            print(f"rebuilding {name}: {reason}")
            continue
        print(f"reusing {name} from {entry.version} (inputs {entry.inputs_hash})")
        reused[name] = ReusedImage(name=name, entry=data[name])
    return reused


def in_publish_registry(artifact: Artifact) -> bool:
    """Whether `artifact` already lives where this run publishes `artifact`'s
    image: a reused release entry, rather than a candidate to promote."""
    from . import publish_images as pi
    if artifact.format == FORMAT_OCI:
        return artifact.url.startswith(f"{pi.oci_ref(artifact.image)}:")
    if pi.ext4_store() == pi.STORE_OCI:
        return artifact.url.startswith(f"{pi.oci_ref(pi.ext4_package(artifact.image))}:")
    base, owner, _ = pi._config()
    return artifact.url.startswith(
        f"{base}/api/packages/{owner}/generic/{pi.ext4_package(artifact.image)}/")


def kept_entry(entry: ImageEntry, raw: dict[str, object]) -> ReusedImage | None:
    """`entry` as promotion keeps it, when it lives wholly in the publish
    registry already (the candidate reused it from a release). Raises
    `PublishError` unless every artifact is still there by digest."""
    from . import publish_images as pi
    if not all(in_publish_registry(a) for a in entry.artifacts):
        return None
    gone = [a for a in entry.artifacts if not artifact_present(a)]
    if gone:
        raise pi.PublishError(
            f"{entry.name}: the tested manifest pins artifacts the publish "
            f"registry no longer has: "
            + ", ".join(f"{a.url}@{a.digest}" for a in gone))
    print(f"  keeping {entry.name} from {entry.version} (inputs {entry.inputs_hash})")
    return ReusedImage(name=entry.name, entry=raw)


def reuse_for_args(
    images: list[str], platforms: list[str], source: Path | None, *,
    repo_root: Path, no_ext4: bool = False,
) -> dict[str, ReusedImage] | None:
    """The images a publish reuses from `source`, or the committed manifest
    when that is None. None means `source` was named but is missing."""
    if source is None and artifacts_manifest_path(repo_root).is_file():
        source = artifacts_manifest_path(repo_root)
    if source is None:
        print("no published-artifacts.json to reuse from; building every image")
        return {}
    if not source.is_file():
        print(f"--reuse-from {source}: no such file", file=sys.stderr)
        return None
    return plan_reuse(images, platforms, source, repo_root=repo_root, no_ext4=no_ext4)
