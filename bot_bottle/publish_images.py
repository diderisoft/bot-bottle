"""Build the fixed images off-host and publish them (PRD: OCI image publishing).

Runs off-host — it needs a container build tool, which launch hosts do not have.
Same split `firecracker.publish_infra` already draws.

Per image, per platform: build, push OCI and record the registry digest, then
export the rootfs to a gzipped ext4 and PUT it as a generic package. Finally
write `published-artifacts.json`.

Interchangeability is enforced here rather than in the manifest, which records
one `inputs_hash` per image and so cannot express disagreement. This is the only
place that sees both the inputs and the artifacts they produced.

One mode, one machine: build and publish everything.

  python3 -m bot_bottle.publish_images [--platform linux/amd64 ...]

Auth: a `write:package` token from `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`, never on a
command line. The container runtime needs its own registry login for the push.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import image_inputs, resources
from .published_artifacts import FORMAT_EXT4, FORMAT_OCI, ARTIFACTS_MANIFEST_NAME

_CHUNK = 1 << 20
_HTTP_TIMEOUT_SECONDS = 60.0

_DEFAULT_BASE = "https://gitea.dideric.is"
_DEFAULT_OWNER = "didericis"

TOKEN_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN"


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
    """The release version every image is published under, from
    `pyproject.toml`.

    One number per release, so images resolved together were built together."""
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
    """The platform this host builds natively — the default when none is given."""
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


def local_tag(image: str, plat: str) -> str:
    """The build-local tag for one image on one platform.

    Deliberately not `:latest`. Builds run concurrently, so a name shared
    between an image's two platforms would race: whichever finished last would
    decide what got pushed and what got packaged."""
    return f"{image}:build-{_platform_suffix(plat)}"


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
    _run([runtime, "tag", local_tag(image, plat), target], what=f"tagging {target}")
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
                    f"already published at {url} (HTTP 409); "
                    f"bump the version to republish"
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


def _head(url: str, token: str) -> int:
    """HTTP status of a HEAD, without downloading the body. 0 if unreachable."""
    req = urllib.request.Request(url, method="HEAD")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def already_published(
    images: list[str], platforms: list[str], version: str, token: str,
) -> list[str]:
    """Artifacts that already exist at `version`, as URLs. Empty when clear.

    Checked before building rather than at upload time. Uploads only start once
    every image is built, so without this a collision on the second image
    surfaces after the whole matrix has been built — ten minutes of work thrown
    away to learn something one HTTP round trip could have said up front."""
    base, owner, _ = _config()
    found: list[str] = []
    for image in images:
        for plat in platforms:
            ext4 = ext4_url(image, version, plat)
            if _head(ext4, token) == 200:
                found.append(ext4)
            # The OCI half needs checking too, and more urgently: `docker push`
            # silently overwrites an existing tag, so nothing downstream would
            # ever report it.
            manifest = (
                f"{base}/v2/{owner}/{image}/manifests/{oci_tag(version, plat)}"
            )
            if _head(manifest, token) == 200:
                found.append(f"{oci_ref(image)}:{oci_tag(version, plat)}")
    return found


def delete_published(
    images: list[str], platforms: list[str], version: str, token: str,
) -> None:
    """Remove everything published at `version` so it can be republished.

    Only reachable via `--force`. Generic packages reject a PUT over an
    existing file, so republishing means deleting first."""
    base, owner, _ = _config()
    for image in images:
        url = f"{base}/api/packages/{owner}/generic/{ext4_package(image)}/{version}"
        for plat in platforms:
            target = f"{url}/{ext4_filename(plat)}"
            req = urllib.request.Request(target, method="DELETE")
            if token:
                req.add_header("Authorization", f"token {token}")
            try:
                with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
                    print(f"  deleted {target}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise PublishError(
                        f"could not delete {target} (HTTP {e.code})"
                    ) from e
            except urllib.error.URLError as e:
                raise PublishError(f"registry unreachable: {target}") from e


def check_interchangeable(built: BuiltImage) -> None:
    """Every artifact of an image must come from one `inputs_hash`, or the
    publish aborts.

    Consumers pick a format by preference, so the formats must be substitutes;
    built from different commits they would not be."""
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


def write_artifacts_manifest(images: list[BuiltImage], dest: Path) -> None:
    """Write `published-artifacts.json` for every built image, sorted for a stable
    diff (this file is committed, and a churning key order would make every
    publish look like a change)."""
    data = {img.name: artifacts_manifest_entry(img) for img in sorted(images, key=lambda i: i.name)}
    dest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {dest}")


def build_ext4(image: str, plat: str, out_dir: Path, inputs: str) -> BuiltArtifact:
    """Export `image`'s filesystem into a bootable, gzipped ext4.

    Delegates to the firecracker rootfs machinery: `build_base_rootfs_dir`
    exports and injects the guest boot bits, `build_rootfs_ext4` populates the
    filesystem with `mke2fs -d`. The infra pair boots a role-specific PID 1;
    agent images take the default init (`init_script=None`)."""
    from .backend.firecracker import infra_vm, util as fc_util

    role = image_inputs.INFRA_ROLES.get(image)
    init = infra_vm.role_init(role) if role else None
    base = fc_util.build_base_rootfs_dir(
        local_tag(image, plat), variant=f"-{image}-{_platform_suffix(plat)}",
        init_script=init,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-platform, like the .gz beside it. An image's two platforms are built
    # concurrently, and `mke2fs -d` populating one path from two processes
    # corrupts the filesystem it is writing ("Directory block checksum does not
    # match") — nondeterministically, only when the two windows overlap.
    ext4 = out_dir / f"{image}.{_platform_suffix(plat)}.ext4"
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
) -> list[BuiltArtifact]:
    """Build, push, and package one image on one platform, returning everything
    produced.

    Both formats come from one build, with a single `inputs_hash` stamped onto
    each — which is what makes them interchangeable."""
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
         "--platform", plat, *build_args, "-t", local_tag(image, plat),
         str(repo_root)],
        what=f"building {image} for {plat}",
    )

    produced = [BuiltArtifact(
        fmt=FORMAT_OCI, platform=plat,
        digest=push_oci(image, version, plat, runtime=runtime),
        locator=oci_tag(version, plat), namespace=oci_ref(image),
        inputs_hash=inputs,
    )]
    produced.append(build_ext4(image, plat, out_dir / image, inputs))
    return produced


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish_images",
        description="Build + publish the fixed images and write published-artifacts.json.",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=Path("published-images"),
        help="scratch directory for rootfs artifacts and the manifest",
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
        "--version", default=None,
        help="publish under this version instead of the one in pyproject.toml. "
             "For rehearsals — use a throwaway like 0.0.0-dev so a trial run "
             "cannot be mistaken for a release.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="delete anything already published at this version and republish. "
             "Without it, an existing version aborts before any build starts.",
    )
    parser.add_argument(
        "--jobs", type=int, default=None,
        help="builds to run concurrently (default: a quarter of the cores, "
             "capped at 8)",
    )
    parser.add_argument("--runtime", default="docker", help="container runtime")
    return parser.parse_args(argv)


def default_jobs() -> int:
    """How many builds to run at once by default.

    Each build occupies roughly one core: emulated builds are dominated by a
    single-threaded qemu, and native ones by serial `apt`/`npm` steps. So the
    machine sits mostly idle building one image at a time — a 24-core host
    measured a load average under 3. Quarter of the cores leaves room for
    buildkit's own parallelism and for anything else on a shared runner."""
    return max(1, min(8, (os.cpu_count() or 4) // 4))


def build_all(
    images: list[str],
    platforms: list[str],
    version: str,
    *,
    repo_root: Path,
    work_dir: Path,
    runtime: str = "docker",
    jobs: int | None = None,
) -> list[BuiltImage]:
    """Build every (image, platform) pair, up to `jobs` at a time.

    The pairs are independent — separate build contexts, separate local tags,
    separate output dirs — so wall time falls to roughly the slowest single
    build rather than the sum of all of them.

    Every failure is collected and raised together: a publish that is going to
    fail should name every image that broke, not stop at the first."""
    if jobs is None:
        jobs = default_jobs()
    tasks = [(name, plat) for name in images for plat in platforms]
    print(f"building {len(tasks)} image/platform pair(s), {jobs} at a time")

    produced: dict[str, list[BuiltArtifact]] = {name: [] for name in images}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(build_image, name, version, plat, repo_root=repo_root,
                        out_dir=work_dir, runtime=runtime): (name, plat)
            for name, plat in tasks
        }
        for future in as_completed(futures):
            name, plat = futures[future]
            try:
                produced[name].extend(future.result())
            # pylint: disable=broad-exception-caught
            # Deliberately broad: one build blowing up in an unforeseen way
            # must not swallow the results of the others, and every failure is
            # re-raised together below.
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name} ({plat}): {exc}")

    if failures:
        raise PublishError(
            f"{len(failures)} build(s) failed:\n  " + "\n  ".join(failures)
        )

    built: list[BuiltImage] = []
    for name in images:
        entry = BuiltImage(name=name, version=version, artifacts=produced[name])
        check_interchangeable(entry)
        built.append(entry)
    return built


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # Builds take minutes and print as they go; block-buffered output would
    # withhold all of it until the end, and interleave wrongly with the
    # unbuffered stderr — so a traceback lands above the work that caused it.
    sys.stdout.reconfigure(line_buffering=True)

    # Before building anything: an OCI push is not undoable, so discovering a
    # missing token afterwards would leave the registry holding images with no
    # matching rootfs or manifest.
    _, _, token = _config()
    if not token:
        print(
            f"{TOKEN_ENV} is not set; a token with write:package on the target "
            f"owner is required to publish",
            file=sys.stderr,
        )
        return 2

    repo_root = resources.build_root()
    version = args.version or repo_version(repo_root)
    images = args.image or list(image_inputs.IMAGES)
    platforms = args.platform or [current_build_platform()]
    work_dir = args.work_dir

    existing = already_published(images, platforms, version, token)
    if existing and not args.force:
        print(
            f"{len(existing)} artifact(s) already published at {version}:\n  "
            + "\n  ".join(existing)
            + "\n  bump the version, or pass --force to replace them.",
            file=sys.stderr,
        )
        return 2
    if existing:
        print(f"--force: removing {len(existing)} artifact(s) at {version}")
        delete_published(images, platforms, version, token)

    built = build_all(images, platforms, version, repo_root=repo_root,
                      work_dir=work_dir, runtime=args.runtime, jobs=args.jobs)

    for entry in built:
        for artifact in entry.artifacts:
            if artifact.fmt != FORMAT_EXT4:
                continue  # OCI was pushed by the build step
            put_generic(
                ext4_url(entry.name, entry.version, artifact.platform),
                work_dir / entry.name / artifact.locator, token,
            )

    dest = work_dir / ARTIFACTS_MANIFEST_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_artifacts_manifest(built, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
