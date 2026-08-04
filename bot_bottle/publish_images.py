"""Build the fixed images off-host and publish them (PRD: OCI image publishing).

The off-host half. This *does* use a container build tool, but never on a launch
host — same split `firecracker.publish_infra` already draws. A launch host only
resolves, downloads and verifies.

Per image, per platform:

    build -> push OCI, record the registry digest
          -> (roles that boot as a VM) export rootfs, mke2fs, gzip,
             PUT as a generic package, record the file digest

then write `published-artifacts.json` mapping each image to its version, its
`inputs_hash`, and a digest per format and platform.

**The interchangeability invariant is enforced here, not in the manifest.** The
schema has one `inputs_hash` per image, so it *cannot express* two artifacts
built from different commits — which means nothing in the manifest can catch it
either. This module is the only place that sees both the build inputs and the
resulting artifacts, so it is where "every artifact of an image was built from
the same inputs" has to be checked. Publishing aborts rather than emit a version
whose formats disagree, because the whole premise of the format fallback is that
either artifact is an acceptable substitute.

  python3 -m bot_bottle.publish_images --output DIR       # build a candidate
  python3 -m bot_bottle.publish_images --publish-dir DIR  # upload it

Auth: a token with `write:package` on the target owner, from
`BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`. Never passed on a command line.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import image_inputs, resources
from .published_artifacts import FORMAT_EXT4, FORMAT_OCI, ARTIFACTS_MANIFEST_NAME

_CHUNK = 1 << 20
_HTTP_TIMEOUT_SECONDS = 60.0

_DEFAULT_BASE = "https://gitea.dideric.is"
_DEFAULT_OWNER = "didericis"

TOKEN_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN"

# Images that also ship a bootable rootfs. Firecracker boots every one of these
# as a microVM — the infra pair as the control/data planes, the agent images as
# the agents themselves — so all of them publish ext4 alongside OCI.
EXT4_IMAGES = frozenset(image_inputs.IMAGES)


class PublishError(RuntimeError):
    """A build, upload, or invariant check failed. Always aborts the publish:
    a partially published version is worse than an unpublished one, because a
    host can resolve it and get an incoherent set."""


@dataclass
class BuiltArtifact:
    fmt: str
    platform: str
    digest: str
    locator: str
    namespace: str
    # The inputs hash this artifact was actually built from — carried per
    # artifact precisely so the cross-format check below has something to
    # compare, rather than trusting that one build equals another.
    inputs_hash: str


@dataclass
class BuiltImage:
    name: str
    version: str
    artifacts: list[BuiltArtifact] = field(default_factory=list)


def _config() -> tuple[str, str, str]:
    base = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_BASE", _DEFAULT_BASE).rstrip("/")
    owner = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_OWNER", _DEFAULT_OWNER)
    token = os.environ.get(TOKEN_ENV, "")
    return base, owner, token


def repo_version(repo_root: Path | None = None) -> str:
    """The release version every image is published under.

    One number describes a coherent set, so a host resolving several images at
    one version gets images that were built together. Read from
    `pyproject.toml` rather than a separate file so it cannot drift from the
    package it ships."""
    if repo_root is None:
        repo_root = resources.build_root()
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    raise PublishError("could not read version from pyproject.toml")


def _platform_suffix(plat: str) -> str:
    """`linux/arm64` -> `arm64`, for tags and filenames."""
    return plat.rsplit("/", 1)[-1]


def current_build_platform() -> str:
    """The platform this host builds natively, the default when none is given.

    Defaulting to the host rather than a fixed value keeps a bare invocation
    honest: whichever runner picks the job builds what it can build fastest,
    and cross-building is something you ask for explicitly."""
    from .published_artifacts import current_platform
    return current_platform()


def oci_ref(image: str) -> str:
    base, owner, _ = _config()
    host = base.split("://", 1)[-1]
    return f"{host}/{owner}/{image}"


def oci_tag(version: str, plat: str) -> str:
    return f"{version}-{_platform_suffix(plat)}"


def ext4_package(image: str) -> str:
    return f"{image}-rootfs"


def ext4_filename(plat: str) -> str:
    return f"rootfs.{_platform_suffix(plat)}.ext4.gz"


def _run(argv: list[str], *, what: str) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, check=False, text=True)
    except OSError as e:
        raise PublishError(f"{what}: {argv[0]} not available ({e})") from e
    if proc.returncode != 0:
        raise PublishError(
            f"{what} failed (exit {proc.returncode}):\n"
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _gzip(src: Path, dest: Path) -> None:
    with open(src, "rb") as fh, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(fh, out, _CHUNK)


def push_oci(image: str, version: str, plat: str, *, runtime: str = "docker") -> str:
    """Push `image` for `plat` and return the digest the registry assigned.

    The digest is read back from the registry rather than computed locally:
    it is the registry's identifier for the manifest it stored, and inventing
    it locally would defeat the point of pinning to it."""
    ref, tag = oci_ref(image), oci_tag(version, plat)
    target = f"{ref}:{tag}"
    _run([runtime, "tag", f"{image}:latest", target], what=f"tagging {target}")
    _run([runtime, "push", target], what=f"pushing {target}")
    out = _run(
        [runtime, "inspect", "--format={{index .RepoDigests 0}}", target],
        what=f"reading digest for {target}",
    ).strip()
    if "@" not in out:
        raise PublishError(f"could not read a digest for {target}: {out!r}")
    return out.rsplit("@", 1)[1]


def put_generic(url: str, body: Path, token: str) -> None:
    """PUT a (hundreds-of-MB) file, streamed rather than buffered."""
    length = body.stat().st_size
    with open(body, "rb") as handle:
        req = urllib.request.Request(url, data=handle, method="PUT")  # type: ignore[arg-type]
        req.add_header("Content-Length", str(length))
        req.add_header("Content-Type", "application/octet-stream")
        if token:
            req.add_header("Authorization", f"token {token}")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                print(f"  uploaded {url} (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise PublishError(
                    f"already published at {url} (HTTP 409); bump the version "
                    f"or pass --force to overwrite"
                ) from e
            raise PublishError(f"upload failed (HTTP {e.code}): {url}") from e
        except urllib.error.URLError as e:
            raise PublishError(f"registry unreachable: {url} ({e.reason})") from e


def ext4_url(image: str, version: str, plat: str) -> str:
    base, owner, _ = _config()
    return (
        f"{base}/api/packages/{owner}/generic/"
        f"{ext4_package(image)}/{version}/{ext4_filename(plat)}"
    )


def check_interchangeable(built: BuiltImage) -> None:
    """Every artifact of an image must come from one `inputs_hash`.

    This is the invariant that makes the format fallback safe. A consumer picks
    ext4 or OCI by preference and availability, so the two must be substitutes;
    if they were built from different commits, which code a host ends up running
    would depend on which format happened to be retained. The manifest cannot
    catch this — it records one hash per image — so it is caught here."""
    hashes = {a.inputs_hash for a in built.artifacts}
    if len(hashes) > 1:
        detail = "\n".join(
            f"    {a.fmt}/{a.platform}: {a.inputs_hash}" for a in built.artifacts
        )
        raise PublishError(
            f"{built.name} {built.version}: artifacts were built from different "
            f"inputs, so they are not interchangeable:\n{detail}\n"
            f"    refusing to publish — rebuild the whole image from one checkout."
        )


def artifacts_manifest_entry(built: BuiltImage) -> dict[str, object]:
    """One image's manifest entry, in the committed schema."""
    check_interchangeable(built)
    artifacts: dict[str, dict[str, object]] = {}
    for artifact in built.artifacts:
        fmt = artifacts.setdefault(
            artifact.fmt,
            {"ref" if artifact.fmt == FORMAT_OCI else "package": artifact.namespace,
             "platforms": {}},
        )
        key = "tag" if artifact.fmt == FORMAT_OCI else "file"
        fmt["platforms"][artifact.platform] = {  # type: ignore[index]
            key: artifact.locator, "digest": artifact.digest,
        }
    return {
        "version": built.version,
        "inputs_hash": next(iter({a.inputs_hash for a in built.artifacts})),
        "artifacts": artifacts,
    }


def merge_artifacts_manifests(parts: list[Path], dest: Path) -> None:
    """Union per-platform manifests from several build hosts into one.

    Each architecture is built on a host that can build it natively, so each
    emits a manifest covering only its own platform. They must be combined
    without either clobbering the other: entries merge per image, per format,
    per platform.

    A conflict — two parts publishing the same image/format/platform with
    different digests — aborts. It means two hosts built the same thing and
    disagreed, so picking either would be arbitrary, and the manifest is
    supposed to be the one place a host can trust."""
    merged: dict[str, dict[str, Any]] = {}
    for part in parts:
        data = json.loads(part.read_text(encoding="utf-8"))
        for image, entry in data.items():
            into = merged.setdefault(image, {
                "version": entry["version"],
                "inputs_hash": entry["inputs_hash"],
                "artifacts": {},
            })
            if into["version"] != entry["version"]:
                raise PublishError(
                    f"{image}: {part} declares version {entry['version']}, "
                    f"another part declares {into['version']}"
                )
            # The inputs_hash legitimately differs per platform (architecture
            # is one of its inputs), so it is recorded per platform below and
            # the image-level value is informational — keep the first seen.
            for fmt, raw_fmt in entry["artifacts"].items():
                target = into["artifacts"].setdefault(
                    fmt, {k: v for k, v in raw_fmt.items() if k != "platforms"}
                    | {"platforms": {}},
                )
                for plat, raw_plat in raw_fmt["platforms"].items():
                    prior = target["platforms"].get(plat)
                    if prior is not None and prior != raw_plat:
                        raise PublishError(
                            f"{image}.{fmt}.{plat}: conflicting artifacts across "
                            f"build hosts ({prior} vs {raw_plat}); two hosts built "
                            f"the same thing and disagreed"
                        )
                    target["platforms"][plat] = raw_plat

    dest.write_text(
        json.dumps(dict(sorted(merged.items())), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"merged {len(parts)} manifest part(s) -> {dest}")


def write_artifacts_manifest(images: list[BuiltImage], dest: Path) -> None:
    """Write `published-artifacts.json` for every built image, sorted for a stable
    diff (this file is committed, and a churning key order would make every
    publish look like a change)."""
    data = {img.name: artifacts_manifest_entry(img) for img in sorted(images, key=lambda i: i.name)}
    dest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {dest}")


def build_ext4(image: str, plat: str, out_dir: Path, inputs: str) -> BuiltArtifact:
    """Export `image`'s filesystem into a bootable, gzipped ext4.

    Reuses the firecracker rootfs machinery rather than reimplementing it:
    `build_base_rootfs_dir` exports the image and injects the guest boot bits,
    and `build_rootfs_ext4` populates a filesystem with `mke2fs -d` (rootless,
    no mount). The infra pair boots a role-specific PID 1; agent images take the
    default agent init, which is what `init_script=None` selects.

    Publishing this is what removes the most expensive per-host operation in the
    system — a local image build followed by a rootfs export — for every host
    that would otherwise do it itself."""
    from .backend.firecracker import infra_vm, util as fc_util

    role = image_inputs.INFRA_ROLES.get(image)
    init = infra_vm.role_init(role) if role else None
    base = fc_util.build_base_rootfs_dir(
        f"{image}:latest", variant=f"-{image}", init_script=init,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    ext4 = out_dir / f"{image}.ext4"
    print(f"  building rootfs for {image} ({plat})")
    fc_util.build_rootfs_ext4(base, ext4)
    gz = out_dir / ext4_filename(plat)
    print(f"  compressing {image} rootfs")
    _gzip(ext4, gz)
    ext4.unlink(missing_ok=True)

    return BuiltArtifact(
        fmt=FORMAT_EXT4, platform=plat,
        digest=f"sha256:{_sha256(gz)}",
        locator=ext4_filename(plat), namespace=ext4_package(image),
        inputs_hash=inputs,
    )


def build_image(
    image: str,
    version: str,
    plat: str,
    *,
    repo_root: Path,
    out_dir: Path,
    runtime: str = "docker",
    with_ext4: bool = True,
) -> list[BuiltArtifact]:
    """Build, push, and (where applicable) publish the rootfs for one image on
    one platform. Returns everything produced, for the manifest.

    Both formats are derived from **one** build, which is what makes them
    interchangeable rather than merely similar — the `inputs_hash` is computed
    once and stamped onto every artifact that came out of it."""
    inputs = image_inputs.inputs_hash(image, repo_root=repo_root)
    print(f"building {image} {version} ({plat}) inputs={inputs}")

    dockerfile = repo_root / image_inputs.DOCKERFILES[image]
    # The digest-pinned base images have no Dockerfile defaults on purpose —
    # they live in image-build-args.json and every supported build path resolves
    # them through this helper. Omitting them does not build an unpinned image,
    # it fails outright, which is the intended shape.
    build_args: list[str] = []
    for name, value in sorted(resources.image_build_args(dockerfile).items()):
        build_args += ["--build-arg", f"{name}={value}"]
    _run(
        [runtime, "build", "-f", str(dockerfile),
         "--platform", plat, *build_args, "-t", f"{image}:latest", str(repo_root)],
        what=f"building {image} for {plat}",
    )

    produced = [BuiltArtifact(
        fmt=FORMAT_OCI, platform=plat,
        digest=push_oci(image, version, plat, runtime=runtime),
        locator=oci_tag(version, plat), namespace=oci_ref(image),
        inputs_hash=inputs,
    )]
    if with_ext4 and image in EXT4_IMAGES:
        produced.append(build_ext4(image, plat, out_dir / image, inputs))
    return produced


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish_images",
        description="Build + publish the fixed images and write published-artifacts.json.",
    )
    parser.add_argument(
        "--output", type=Path,
        help="build a candidate bundle into DIR without uploading",
    )
    parser.add_argument(
        "--publish-dir", type=Path,
        help="upload a previously built candidate bundle from DIR",
    )
    parser.add_argument(
        "--image", action="append", default=None,
        help="restrict to one image (repeatable); default is every known image",
    )
    parser.add_argument(
        "--platform", action="append", default=None,
        help="platform to build (repeatable), e.g. linux/arm64",
    )
    parser.add_argument(
        "--skip-ext4", action="store_true",
        help="build and push OCI only; skip rootfs packaging (for a build host "
             "without e2fsprogs, e.g. macOS)",
    )
    parser.add_argument(
        "--ext4-from-manifest", type=Path, default=None,
        help="package rootfs from images already published in MANIFEST, pulling "
             "each by its pinned digest instead of building. Sound cross-arch: "
             "export never starts the container.",
    )
    parser.add_argument(
        "--merge", action="append", type=Path, default=None,
        metavar="MANIFEST", help="manifest part to merge (repeatable)",
    )
    parser.add_argument(
        "--merge-into", type=Path, default=None,
        help="where to write the merged manifest",
    )
    parser.add_argument("--runtime", default="docker", help="container runtime")
    return parser.parse_args(argv)


def package_from_manifest(
    manifest: Path, out_dir: Path, plat: str, *, runtime: str = "docker",
) -> list[BuiltImage]:
    """Package rootfs artifacts for images already published to the registry.

    Pulls each by its **pinned digest** — not its tag — so what gets packaged is
    exactly what was published, and tags the result locally so the shared rootfs
    path can export it. This is how one host packages another architecture's
    rootfs: `docker create` + `docker export` never start the container, so no
    foreign-arch binary runs."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    built: list[BuiltImage] = []
    for image, entry in sorted(data.items()):
        oci = entry["artifacts"].get(FORMAT_OCI, {}).get("platforms", {}).get(plat)
        if oci is None:
            print(f"  {image}: no {plat} oci artifact in {manifest}, skipping")
            continue
        ref = f"{entry['artifacts'][FORMAT_OCI]['ref']}@{oci['digest']}"
        print(f"pulling {image} {plat} by digest for rootfs packaging")
        _run([runtime, "pull", "--platform", plat, ref], what=f"pulling {ref}")
        _run([runtime, "tag", ref, f"{image}:latest"], what=f"tagging {image}")
        produced = BuiltImage(name=image, version=entry["version"])
        produced.artifacts.append(
            build_ext4(image, plat, out_dir / image, entry["inputs_hash"])
        )
        # Carry the OCI artifact through unchanged so the emitted part still
        # describes both formats for this platform.
        produced.artifacts.append(BuiltArtifact(
            fmt=FORMAT_OCI, platform=plat, digest=oci["digest"],
            locator=oci["tag"], namespace=entry["artifacts"][FORMAT_OCI]["ref"],
            inputs_hash=entry["inputs_hash"],
        ))
        built.append(produced)
    return built


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.merge:
        if not args.merge_into:
            print("--merge requires --merge-into", file=sys.stderr)
            return 2
        merge_artifacts_manifests(args.merge, args.merge_into)
        return 0

    if not args.output and not args.publish_dir:
        print(
            "one of --output / --publish-dir / --merge is required",
            file=sys.stderr,
        )
        return 2

    repo_root = resources.build_root()
    version = repo_version(repo_root)
    images = args.image or list(image_inputs.IMAGES)
    platforms = args.platform or [current_build_platform()]

    out_dir = args.output or args.publish_dir
    built: list[BuiltImage] = []

    if args.ext4_from_manifest:
        for plat in platforms:
            built.extend(package_from_manifest(
                args.ext4_from_manifest, out_dir, plat, runtime=args.runtime,
            ))
    else:
        for name in images:
            entry = BuiltImage(name=name, version=version)
            for plat in platforms:
                entry.artifacts.extend(
                    build_image(name, version, plat, repo_root=repo_root,
                                out_dir=out_dir, runtime=args.runtime,
                                with_ext4=not args.skip_ext4)
                )
            # Check per image as it completes, so a mismatch aborts before the
            # next image is built rather than after the whole run.
            check_interchangeable(entry)
            built.append(entry)

    if args.publish_dir:
        _, _, token = _config()
        if not token:
            raise PublishError(
                f"{TOKEN_ENV} is not set; a token with write:package on the "
                f"target owner is required to upload"
            )
        for entry in built:
            for artifact in entry.artifacts:
                if artifact.fmt != FORMAT_EXT4:
                    continue  # OCI was pushed by the build step
                gz = args.publish_dir / entry.name / artifact.locator
                put_generic(
                    ext4_url(entry.name, entry.version, artifact.platform),
                    gz, token,
                )

    dest = out_dir / ARTIFACTS_MANIFEST_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_artifacts_manifest(built, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
