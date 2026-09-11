"""The published-artifacts manifest (`published-artifacts.json`).

`image-build-args.json` pins what goes *in* to an image; this records what came
*out*. Per image: a legible `version`, an `inputs_hash` (see `image_inputs`), and
one asset per (type, platform). Only the digest is verified — the version is
human-facing and never resolved on alone.

Each asset is a flat, self-describing `{type, platform, url, digest}` keyed by
`<type>_<platform>`. `type` says how to fetch and what `url`/`digest` mean: `oci`
→ `url` is a registry pull ref (`host/owner/name:tag`) and `digest` is the OCI
manifest digest; `ext4` → `url` is an HTTPS download and `digest` is the file's
sha256. Platform is dash-spelled on disk (`linux-arm64`) and slash-spelled in
code (`linux/arm64`); the translation happens only at this parse/serialize
boundary. The loader asserts `key == f"{type}_{platform}"`, so the key/field
redundancy is an integrity check rather than two things to keep in sync.

Read-only: loading, validating, resolving. `publish_images` produces the
manifest; `image_registry` fetches and verifies what it points at.

Named `ARTIFACTS_MANIFEST_*` / `*_artifacts_manifest` throughout to stay distinct
from bottle manifests (`bot_bottle.manifest`) and OCI manifests.
"""

from __future__ import annotations

import json
import platform as _platform
import re
from dataclasses import dataclass
from pathlib import Path

from . import resources

ARTIFACTS_MANIFEST_NAME = "published-artifacts.json"

# Distribution formats, in no particular order — consumers declare their own
# preference (see `resolve`).
FORMAT_OCI = "oci"
FORMAT_EXT4 = "ext4"
FORMATS = (FORMAT_OCI, FORMAT_EXT4)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_INPUTS_HASH = re.compile(r"^[0-9a-f]{16}$")
_MANIFEST_PLATFORM = re.compile(r"^linux-(amd64|arm64)$")   # on-disk (dash)


def _platform_from_manifest(dashed: str) -> str:
    """`linux-arm64` (on disk) -> `linux/arm64` (in code)."""
    return dashed.replace("-", "/", 1)


def platform_to_manifest(slashed: str) -> str:
    """`linux/arm64` (in code) -> `linux-arm64` (on disk). Public so the
    publisher writes the same spelling the loader validates."""
    return slashed.replace("/", "-", 1)

# `platform.machine()` spellings we accept for each canonical platform string.
_MACHINE_ALIASES = {
    "x86_64": "linux/amd64",
    "amd64": "linux/amd64",
    "aarch64": "linux/arm64",
    "arm64": "linux/arm64",
}


class ArtifactsManifestError(RuntimeError):
    """`published-artifacts.json` is missing, malformed, or internally inconsistent.

    Raised rather than `die`d so callers can decide: a launch path fails
    closed, while the CI checker collects every problem before reporting."""


@dataclass(frozen=True)
class Artifact:
    """One image asset: one `format` (the manifest's `type`), one platform.

    `url` is the whole locator — for `oci` a registry reference
    `host/owner/name:tag`, for `ext4` the generic-package download URL. `digest`
    is always what gets verified (an OCI manifest digest, or the ext4 file's
    sha256). `platform` is the in-code slash form (`linux/arm64`)."""

    image: str
    format: str
    platform: str
    url: str
    digest: str
    # Carried so an Artifact is enough to fetch/verify on its own.
    version: str = ""


@dataclass(frozen=True)
class ImageEntry:
    """Every published artifact for one image at one version."""

    name: str
    version: str
    inputs_hash: str
    artifacts: tuple[Artifact, ...]

    def for_format(self, fmt: str, plat: str) -> "Artifact | None":
        for artifact in self.artifacts:
            if artifact.format == fmt and artifact.platform == plat:
                return artifact
        return None


def current_platform() -> str:
    """This host's canonical platform string (`linux/arm64`, `linux/amd64`).

    Keyed on the machine only: every image we publish is a Linux image, even
    when the host running the launcher is macOS (the containers and microVMs
    are Linux regardless)."""
    machine = _platform.machine().lower()
    plat = _MACHINE_ALIASES.get(machine)
    if plat is None:
        raise ArtifactsManifestError(
            f"unsupported machine {machine!r}: no published images for it "
            f"(known: {', '.join(sorted(set(_MACHINE_ALIASES.values())))})"
        )
    return plat


def artifacts_manifest_path(repo_root: Path | None = None) -> Path:
    if repo_root is None:
        repo_root = resources.build_root()
    return repo_root / ARTIFACTS_MANIFEST_NAME


def parse_artifacts_manifest(
    data: object, *, problems: list[str] | None = None,
) -> dict[str, ImageEntry]:
    """Validate `data` and lift it into `ImageEntry`s keyed by image name.

    With `problems`, every fault is collected and parsing continues (the CI
    checker wants the full list); without it, the first fault raises."""
    collect = problems is not None
    found: list[str] = [] if problems is None else problems

    def fail(message: str) -> None:
        if collect:
            found.append(message)
        else:
            raise ArtifactsManifestError(message)

    if not isinstance(data, dict):
        fail(f"{ARTIFACTS_MANIFEST_NAME}: top level must be an object")
        return {}

    entries: dict[str, ImageEntry] = {}
    for name, raw in data.items():
        if not isinstance(raw, dict):
            fail(f"{name}: entry must be an object")
            continue

        version = raw.get("version")
        if not isinstance(version, str) or not version:
            fail(f"{name}: missing version")
            version = ""
        elif version == "latest":
            fail(f"{name}: version must not be 'latest'")

        inputs_hash = raw.get("inputs_hash")
        if not isinstance(inputs_hash, str) or not _INPUTS_HASH.fullmatch(inputs_hash):
            fail(f"{name}: inputs_hash must be 16 lowercase hex chars, got {inputs_hash!r}")
            inputs_hash = ""

        artifacts: list[Artifact] = []
        raw_artifacts = raw.get("artifacts")
        if not isinstance(raw_artifacts, dict) or not raw_artifacts:
            fail(f"{name}: artifacts must be a non-empty object")
            raw_artifacts = {}

        for asset_key, raw_asset in raw_artifacts.items():
            if not isinstance(raw_asset, dict):
                fail(f"{name}.{asset_key}: must be an object")
                continue
            fmt = raw_asset.get("type")
            if fmt not in FORMATS:
                fail(f"{name}.{asset_key}: unknown type {fmt!r} (known: {', '.join(FORMATS)})")
                continue
            plat = raw_asset.get("platform")
            if not isinstance(plat, str) or not _MANIFEST_PLATFORM.fullmatch(plat):
                fail(
                    f"{name}.{asset_key}: platform must be linux-amd64 or "
                    f"linux-arm64, got {plat!r}"
                )
                continue
            expected_key = f"{fmt}_{plat}"
            if asset_key != expected_key:
                fail(
                    f"{name}.{asset_key}: key must equal <type>_<platform> "
                    f"({expected_key!r})"
                )
                continue
            url = raw_asset.get("url")
            if not isinstance(url, str) or not url:
                fail(f"{name}.{asset_key}: missing url")
                continue
            digest = raw_asset.get("digest")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                fail(
                    f"{name}.{asset_key}: digest must be "
                    f"sha256:<64 lowercase hex>, got {digest!r}"
                )
                continue
            artifacts.append(Artifact(
                image=name, format=fmt, platform=_platform_from_manifest(plat),
                url=url, digest=digest, version=version,
            ))

        if artifacts:
            entries[name] = ImageEntry(
                name=name, version=version,
                inputs_hash=inputs_hash, artifacts=tuple(artifacts),
            )

    return entries


def load_artifacts_manifest(repo_root: Path | None = None) -> dict[str, ImageEntry]:
    """Read + validate the committed manifest. Raises `ArtifactsManifestError` if it is
    absent or malformed — a launch path must not silently proceed without it."""
    path = artifacts_manifest_path(repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ArtifactsManifestError(
            f"{path} not found; publish images first "
            f"(python3 -m bot_bottle.publish_images)"
        ) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ArtifactsManifestError(f"{path}: invalid JSON ({e})") from e
    return parse_artifacts_manifest(data)


def load_artifacts_manifest_optional(
    repo_root: Path | None = None,
) -> dict[str, ImageEntry] | None:
    """Like `load_artifacts_manifest`, but returns None when the manifest is
    absent instead of raising.

    For the consumer *pull* path, which falls back to a local build when nothing
    is pinned yet — there is no manifest before the first release. A
    present-but-malformed manifest still raises: that is corruption, not absence."""
    if not artifacts_manifest_path(repo_root).is_file():
        return None
    return load_artifacts_manifest(repo_root)


def resolve(
    entries: dict[str, ImageEntry],
    name: str,
    *,
    prefer: tuple[str, ...],
    plat: str | None = None,
) -> Artifact:
    """The best published artifact for `name` on `plat`, taking the first
    format in `prefer` that exists.

    `prefer` is per-caller: firecracker prefers `ext4` over `oci`, docker and
    macos-container take `oci` only. Falling back rather than failing lets an
    image ship only some formats."""
    if plat is None:
        plat = current_platform()
    entry = entries.get(name)
    if entry is None:
        raise ArtifactsManifestError(
            f"no published image {name!r} in {ARTIFACTS_MANIFEST_NAME} "
            f"(have: {', '.join(sorted(entries)) or '<none>'})"
        )
    for fmt in prefer:
        artifact = entry.for_format(fmt, plat)
        if artifact is not None:
            return artifact
    available = sorted({f"{a.format} {a.platform}" for a in entry.artifacts})
    raise ArtifactsManifestError(
        f"{name} {entry.version} has no artifact for {plat} in any of "
        f"{', '.join(prefer)}; published: {', '.join(available)}"
    )


def check_consistency(entries: dict[str, ImageEntry]) -> list[str]:
    """Cross-artifact problems `parse_artifacts_manifest` cannot see field by
    field. Empty when clean.

    Checks that no two artifacts claim the same digest: different formats and
    platforms are never the same bytes, so a collision means a publish bug
    copied one across. (Interchangeability is enforced in `publish_images`,
    which sees the builds.)"""
    problems: list[str] = []
    for name, entry in sorted(entries.items()):
        seen: dict[str, tuple[str, str]] = {}
        for artifact in entry.artifacts:
            prior = seen.get(artifact.digest)
            if prior is not None:
                problems.append(
                    f"{name}: {artifact.format}/{artifact.platform} and "
                    f"{prior[0]}/{prior[1]} share digest {artifact.digest} — "
                    f"different formats/platforms are never the same bytes"
                )
            seen[artifact.digest] = (artifact.format, artifact.platform)
    return problems


__all__ = [
    "Artifact",
    "FORMATS",
    "FORMAT_EXT4",
    "FORMAT_OCI",
    "ImageEntry",
    "ARTIFACTS_MANIFEST_NAME",
    "ArtifactsManifestError",
    "check_consistency",
    "current_platform",
    "load_artifacts_manifest",
    "load_artifacts_manifest_optional",
    "artifacts_manifest_path",
    "parse_artifacts_manifest",
    "resolve",
]
