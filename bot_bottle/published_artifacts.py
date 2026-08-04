"""The published-artifacts manifest (`published-artifacts.json`).

`image-build-args.json` pins what goes *in* to an image (base images, by tag and
digest). This pins what comes *out*: for every image we publish, a legible
version, a content hash of its build inputs, and a digest per distribution
format and platform.

**"Manifest" is three different things in this codebase; this is the third.**
A *bottle* manifest (`bot_bottle.manifest`) is the user-authored declaration of
what a bottle contains. An *OCI* manifest is the registry document a digest
addresses — the thing `image_registry` resolves a tag to. This module's
artifacts manifest is neither: it is our own committed record of what we
published. Everything here is spelled `ARTIFACTS_MANIFEST_*` /
`*_artifacts_manifest` so a reader never has to infer which sense is meant.

Three fields do three separate jobs, and conflating any two is the failure this
schema exists to prevent:

  * ``version``      — human-facing. Legible in logs, tags and issues. Never
                       trusted for resolution on its own.
  * ``inputs_hash``  — computable from a checkout *before* anything is built, so
                       a host can ask "does the published artifact match my
                       code?", and so two formats of one image can be proven
                       interchangeable.
  * ``digest``       — what is actually verified. Registry- or file-derived, and
                       only knowable *after* a build.

Artifacts nest by format and then platform. That keeps each locator beside its
own hash (no ambiguity about which bytes a digest covers — the OCI digest is
over a *manifest*, the ext4 digest over the published ``.gz`` file), lets an
image legitimately ship only some formats, and lets a consumer walk a preference
order instead of branching on which fields happen to be present.

This module is read-only: loading, validating and resolving. Producing the
manifest is `publish_images.py`; fetching and verifying what it points at is
`image_registry.py`.
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
_PLATFORM = re.compile(r"^linux/(amd64|arm64)$")

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
    """One image, in one format, for one platform.

    `locator` is the format-specific addressing detail — for `oci` the tag, for
    `ext4` the published filename. `digest` is always what gets verified."""

    image: str
    format: str
    platform: str
    digest: str
    locator: str
    # Format-scoped addressing shared by every platform: the OCI repository
    # reference, or the generic-package name for ext4.
    namespace: str


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


def _require(cond: bool, message: str, problems: list[str]) -> bool:
    if not cond:
        problems.append(message)
    return cond


def parse_artifacts_manifest(
    data: object, *, problems: list[str] | None = None,
) -> dict[str, ImageEntry]:
    """Validate `data` and lift it into `ImageEntry`s keyed by image name.

    When `problems` is given, every fault is appended to it and parsing
    continues as far as it can — the CI checker wants the full list. When it is
    not, the first fault raises, which is what a launch path wants."""
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

        for fmt, raw_fmt in raw_artifacts.items():
            if fmt not in FORMATS:
                fail(f"{name}: unknown format {fmt!r} (known: {', '.join(FORMATS)})")
                continue
            if not isinstance(raw_fmt, dict):
                fail(f"{name}.{fmt}: must be an object")
                continue
            namespace = raw_fmt.get("ref" if fmt == FORMAT_OCI else "package")
            if not isinstance(namespace, str) or not namespace:
                key = "ref" if fmt == FORMAT_OCI else "package"
                fail(f"{name}.{fmt}: missing {key}")
                namespace = ""
            platforms = raw_fmt.get("platforms")
            if not isinstance(platforms, dict) or not platforms:
                fail(f"{name}.{fmt}: platforms must be a non-empty object")
                continue
            for plat, raw_plat in platforms.items():
                if not _PLATFORM.fullmatch(plat):
                    fail(f"{name}.{fmt}: unsupported platform {plat!r}")
                    continue
                if not isinstance(raw_plat, dict):
                    fail(f"{name}.{fmt}.{plat}: must be an object")
                    continue
                digest = raw_plat.get("digest")
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    fail(
                        f"{name}.{fmt}.{plat}: digest must be "
                        f"sha256:<64 lowercase hex>, got {digest!r}"
                    )
                    continue
                locator = raw_plat.get("tag" if fmt == FORMAT_OCI else "file")
                if not isinstance(locator, str) or not locator:
                    key = "tag" if fmt == FORMAT_OCI else "file"
                    fail(f"{name}.{fmt}.{plat}: missing {key}")
                    continue
                artifacts.append(Artifact(
                    image=name, format=fmt, platform=plat,
                    digest=digest, locator=locator, namespace=namespace,
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


def resolve(
    entries: dict[str, ImageEntry],
    name: str,
    *,
    prefer: tuple[str, ...],
    plat: str | None = None,
) -> Artifact:
    """The best published artifact for `name` on `plat`, walking `prefer` in
    order and taking the first format that exists.

    `prefer` is the caller's format preference, not a global one: firecracker
    prefers `ext4` (a download and a gunzip) over `oci` (extract layers, apply
    whiteouts, mke2fs, populate a filesystem), while docker and macos-container
    take `oci` only. Falling back rather than failing is what lets an image ship
    only some formats — and what makes narrow ext4 retention safe, since a
    collected rootfs degrades a host to slow rather than broken."""
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
    """Cross-artifact invariants `parse_artifacts_manifest` cannot see field by field.
    Returns a list of problems, empty when clean.

    Note the interchangeability invariant — every artifact of an image built
    from the same inputs — is *structural* here: `inputs_hash` is one field per
    image, so the manifest cannot express disagreement. Enforcing that the
    artifacts were genuinely built from that hash is the publisher's job
    (`publish_images`), since it needs the build, not the manifest.

    What is checkable here is that no two artifacts claim the same digest. A
    digest identifies bytes, and two different formats — or one format on two
    platforms — are never the same bytes. A collision means a publish bug
    copied a digest across entries, which would silently hand a host the wrong
    architecture or the wrong format."""
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
    "artifacts_manifest_path",
    "parse_artifacts_manifest",
    "resolve",
]
