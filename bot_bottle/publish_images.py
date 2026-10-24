"""Build the fixed images and publish them, off-host (launch hosts have no
build tool).

Per image and platform: build, push the OCI image and record its digest, export
a gzipped ext4 and PUT it as a generic package. Then write
`published-artifacts.json`. Formats are checked to share one `inputs_hash`.

  python3 -m bot_bottle.publish_images [--platform linux/amd64 ...]

Auth: a `write:package` token in `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`; the
container runtime needs its own registry login.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .compat import PROTOCOL_VERSION
from . import image_inputs, resources
from .published_artifacts import (
    ARTIFACTS_MANIFEST_NAME,
    Artifact,
    FORMAT_EXT4,
    FORMAT_OCI,
    platform_to_manifest,
)

_CHUNK = 1 << 20
_HTTP_TIMEOUT_SECONDS = 60.0
# The registry hashes a large rootfs before answering.
_UPLOAD_TIMEOUT_SECONDS = 900.0

_DEFAULT_BASE = "https://gitea.dideric.is"
_DEFAULT_OWNER = "didericis"

TOKEN_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_TOKEN"


class PublishError(RuntimeError):
    """A build, upload or invariant check failed. Aborts the publish: a partial
    version is worse than none."""


@dataclass
class BuiltArtifact:
    fmt: str
    platform: str
    digest: str
    locator: str
    namespace: str
    # What this artifact was built from, compared across formats.
    inputs_hash: str


@dataclass
class BuiltImage:
    name: str
    version: str
    artifacts: list[BuiltArtifact] = field(default_factory=list)
    # The protocol these bytes speak: the source's at build time, or the tested
    # manifest's when promoting.
    protocol: int = PROTOCOL_VERSION


def _config() -> tuple[str, str, str]:
    base = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_BASE", _DEFAULT_BASE).rstrip("/")
    owner = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_OWNER", _DEFAULT_OWNER)
    token = os.environ.get(TOKEN_ENV, "")
    return base, owner, token


def repo_version(repo_root: Path | None = None) -> str:
    """The release version every image is published under, from
    `pyproject.toml`."""
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
    """The platform this host builds natively."""
    from .published_artifacts import current_platform
    return current_platform()


def source_url() -> str:
    """The repository the images are built from, set as
    `org.opencontainers.image.source` so Gitea links containers to the repo.
    Independent of the artifact owner a rehearsal may override."""
    return os.environ.get(
        "BOT_BOTTLE_IMAGE_SOURCE_URL", f"{_DEFAULT_BASE}/didericis/bot-bottle",
    )


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
    """Build-local tag for one image and platform. Not `:latest`, since the
    platforms build concurrently."""
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
    """Push `image` for `plat` and return the registry's digest."""
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
    """PUT a large file, streamed."""
    length = body.stat().st_size
    with open(body, "rb") as handle:
        req = urllib.request.Request(url, data=handle, method="PUT")  # type: ignore[arg-type]
        req.add_header("Content-Length", str(length))
        req.add_header("Content-Type", "application/octet-stream")
        if token:
            req.add_header("Authorization", f"token {token}")
        try:
            with urllib.request.urlopen(req, timeout=_UPLOAD_TIMEOUT_SECONDS) as resp:
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
        except TimeoutError as e:
            raise PublishError(
                f"upload of {url} got no answer within {_UPLOAD_TIMEOUT_SECONDS:.0f}s") from e


def ext4_url(image: str, version: str, plat: str) -> str:
    base, owner, _ = _config()
    return (
        f"{base}/api/packages/{owner}/generic/"
        f"{ext4_package(image)}/{version}/{ext4_filename(plat)}"
    )


def _head(url: str, token: str, *, basic: bool = False) -> int:
    """HTTP status of a HEAD, or 0 if unreachable. `basic` uses HTTP Basic,
    which the OCI `/v2/` API needs; `/api/packages` keeps the `token` scheme."""
    req = urllib.request.Request(url, method="HEAD")
    if token:
        if basic:
            creds = base64.b64encode(f"bot-bottle:{token}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
        else:
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
    """URLs of artifacts already published at `version`. Checked before
    building so a collision fails fast."""
    base, owner, _ = _config()
    found: list[str] = []
    for image in images:
        for plat in platforms:
            ext4 = ext4_url(image, version, plat)
            if _head(ext4, token) == 200:
                found.append(ext4)
            # `docker push` silently overwrites a tag, so check OCI too.
            manifest = (
                f"{base}/v2/{owner}/{image}/manifests/{oci_tag(version, plat)}"
            )
            if _head(manifest, token, basic=True) == 200:
                found.append(f"{oci_ref(image)}:{oci_tag(version, plat)}")
    return found


_RELEASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RELEASE_PLATFORMS = ["linux/amd64", "linux/arm64"]


def next_release_version(start: str, token: str) -> str:
    """The first unpublished version from `start` up, bumping the patch. Any
    image or arch already at a version takes it."""
    match = _RELEASE_VERSION.match(start)
    if not match:
        raise PublishError(f"{start!r} is not a MAJOR.MINOR.PATCH release version")
    major, minor, patch = (int(g) for g in match.groups())
    images = list(image_inputs.IMAGES)
    while True:
        version = f"{major}.{minor}.{patch}"
        if not already_published(images, _RELEASE_PLATFORMS, version, token):
            return version
        patch += 1


def delete_published(
    images: list[str], platforms: list[str], version: str, token: str,
) -> None:
    """Remove everything at `version` so it can be republished (`--force`).
    Generic packages reject a PUT over an existing file."""
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


def link_to_repo(images: list[str], token: str) -> None:
    """Link every published package to the repository, so it shows on the
    repo's Packages tab. Both types are linked explicitly, since Gitea reads
    `org.opencontainers.image.source` only when it creates a package. Best
    effort: a failure leaves the artifacts published."""
    base, owner, _ = _config()
    repo = source_url().rstrip("/").rsplit("/", 1)[-1]
    packages = [("container", image) for image in images]
    packages += [("generic", ext4_package(image)) for image in images]

    for kind, package in packages:
        url = f"{base}/api/v1/packages/{owner}/{kind}/{package}/-/link/{repo}"
        req = urllib.request.Request(url, data=b"", method="POST")
        if token:
            req.add_header("Authorization", f"token {token}")
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS):
                print(f"  linked {kind}/{package} -> {owner}/{repo}")
        except urllib.error.HTTPError as e:
            # 400 means already linked.
            if e.code not in (400, 409):
                print(f"  warning: could not link {kind}/{package} "
                      f"(HTTP {e.code})", file=sys.stderr)
        except urllib.error.URLError as e:
            print(f"  warning: could not link {kind}/{package} ({e.reason})",
                  file=sys.stderr)


def check_interchangeable(built: BuiltImage) -> None:
    """Abort unless every artifact of an image shares one `inputs_hash`, since
    consumers treat formats as substitutes."""
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
    """One image's manifest entry: a `{type, platform, url, digest}` asset per
    (type, platform), keyed `<type>_<platform>`."""
    check_interchangeable(built)
    artifacts: dict[str, dict[str, object]] = {}
    for a in built.artifacts:
        plat = platform_to_manifest(a.platform)
        if a.fmt == FORMAT_OCI:
            url = f"{a.namespace}:{a.locator}"
        else:
            url = ext4_url(built.name, built.version, a.platform)
        artifacts[f"{a.fmt}_{plat}"] = {
            "type": a.fmt, "platform": plat, "url": url, "digest": a.digest,
        }
    return {
        "version": built.version,
        "inputs_hash": next(iter({a.inputs_hash for a in built.artifacts})),
        "protocol": built.protocol,
        "artifacts": artifacts,
    }


def write_artifacts_manifest(images: list[BuiltImage], dest: Path) -> None:
    """Write `published-artifacts.json`, sorted for a stable diff."""
    data = {img.name: artifacts_manifest_entry(img) for img in sorted(images, key=lambda i: i.name)}
    dest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {dest}")


def rootfs_source_tag(
    image: str, plat: str, *, repo_root: Path, runtime: str = "docker",
) -> str:
    """The local image `image`'s rootfs is exported from. For the orchestrator
    that's `Dockerfile.orchestrator.fc` (`FROM` the OCI image plus buildah),
    built here."""
    dockerfile = image_inputs.rootfs_dockerfile(image)
    if dockerfile == image_inputs.DOCKERFILES[image]:
        return local_tag(image, plat)

    tag = f"{image}-rootfs:build-{_platform_suffix(plat)}"
    build_args: list[str] = []
    for name, value in sorted(
        resources.image_build_args(repo_root / dockerfile).items()
    ):
        build_args += ["--build-arg", f"{name}={value}"]

    # The derived Dockerfile's base, passed as a digest-pinned local ref (the
    # ARG has no default).
    base_arg = image_inputs.ROOTFS_BASE_ARG.get(image)
    if base_arg:
        from .backend.docker import util as docker_util
        pinned = docker_util.pinned_local_image_ref(local_tag(image, plat))
        build_args += ["--build-arg", f"{base_arg}={pinned}"]
    print(f"  building {dockerfile} for {image} ({plat})")
    _run(
        [runtime, "build", "-f", str(repo_root / dockerfile),
         "--platform", plat, *build_args, "-t", tag, str(repo_root)],
        what=f"building {dockerfile} for {plat}",
    )
    return tag


def build_ext4(
    image: str,
    plat: str,
    out_dir: Path,
    inputs: str,
    *,
    repo_root: Path,
    runtime: str = "docker",
) -> BuiltArtifact:
    """Export `image`'s filesystem into a bootable, gzipped ext4 via the
    firecracker rootfs builder. Infra images boot a role-specific PID 1; agent
    images the default init."""
    from .backend.firecracker import infra_vm, util as fc_util
    from .cli.settings import settings_from_env

    role = image_inputs.INFRA_ROLES.get(image)
    init = infra_vm.role_init(role) if role else None
    source = rootfs_source_tag(image, plat, repo_root=repo_root, runtime=runtime)
    base = fc_util.build_base_rootfs_dir(
        source, settings_from_env().infra, variant=f"-{image}-{_platform_suffix(plat)}",
        init_script=init,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-platform: concurrent `mke2fs -d` into one path corrupts it.
    ext4 = out_dir / f"{image}.{_platform_suffix(plat)}.ext4"
    print(f"  building rootfs for {image} ({plat})")
    fc_util.build_rootfs_ext4(
        base, ext4, slack_mib=image_inputs.rootfs_slack_mib(image))
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
    """Build, push and package one image on one platform. Both formats come
    from one build and carry its `inputs_hash`."""
    inputs = image_inputs.inputs_hash(image, repo_root=repo_root)
    print(f"building {image} {version} ({plat}) inputs={inputs}")

    dockerfile = repo_root / image_inputs.DOCKERFILES[image]
    # Digest-pinned bases have no Dockerfile defaults; they come from
    # image-build-args.json, and omitting them fails the build.
    build_args: list[str] = []
    for name, value in sorted(resources.image_build_args(dockerfile).items()):
        build_args += ["--build-arg", f"{name}={value}"]
    _run(
        [runtime, "build", "-f", str(dockerfile),
         "--platform", plat, *build_args,
         "--label", f"org.opencontainers.image.source={source_url()}",
         "--label", f"org.opencontainers.image.version={version}",
         "-t", local_tag(image, plat), str(repo_root)],
        what=f"building {image} for {plat}",
    )

    produced = [BuiltArtifact(
        fmt=FORMAT_OCI, platform=plat,
        digest=push_oci(image, version, plat, runtime=runtime),
        locator=oci_tag(version, plat), namespace=oci_ref(image),
        inputs_hash=inputs,
    )]
    produced.append(build_ext4(image, plat, out_dir / image, inputs,
                               repo_root=repo_root, runtime=runtime))
    return produced


def promote(
    manifest: Path, to_version: str, token: str, *, runtime: str = "docker",
) -> list[BuiltImage]:
    """Copy every artifact in a tested manifest to `to_version`, never
    rebuilding.

    OCI images are re-tagged from their digest, which must come back unchanged.
    Generic packages have no copy API, so their bytes are downloaded (verified)
    and re-uploaded."""
    from .published_artifacts import parse_artifacts_manifest

    entries = parse_artifacts_manifest(json.loads(manifest.read_text()))
    promoted: list[BuiltImage] = []
    for name, entry in sorted(entries.items()):
        out = BuiltImage(name=name, version=to_version, protocol=entry.protocol)
        for artifact in entry.artifacts:
            if artifact.format == FORMAT_OCI:
                digest = _promote_oci(artifact, to_version, runtime=runtime)
            else:
                digest = _promote_ext4(artifact, to_version, token)
            if digest != artifact.digest:
                raise PublishError(
                    f"{name} {artifact.format}/{artifact.platform}: promoted "
                    f"digest {digest} does not match the tested "
                    f"{artifact.digest} — refusing to ship untested bytes"
                )
            is_oci = artifact.format == FORMAT_OCI
            out.artifacts.append(BuiltArtifact(
                fmt=artifact.format, platform=artifact.platform, digest=digest,
                locator=(oci_tag(to_version, artifact.platform) if is_oci
                         else ext4_filename(artifact.platform)),
                # OCI ref from the tested manifest; ext4's namespace is unused
                # by the writer.
                namespace=(artifact.url.rsplit(":", 1)[0] if is_oci
                           else ext4_package(artifact.image)),
                inputs_hash=entry.inputs_hash,
            ))
        promoted.append(out)
    return promoted


def _promote_oci(
    artifact: "Artifact", to_version: str, *, runtime: str = "docker",
) -> str:
    """Re-tag a published image at the new version (pulled by digest) and
    return its digest."""
    # From the tested manifest, so promote copies exactly what passed.
    ref = artifact.url.rsplit(":", 1)[0]
    source = f"{ref}@{artifact.digest}"
    target = f"{ref}:{oci_tag(to_version, artifact.platform)}"
    print(f"  promoting {artifact.image} {artifact.platform} -> {target}")
    _run([runtime, "pull", "--platform", artifact.platform, source],
         what=f"pulling {source}")
    _run([runtime, "tag", source, target], what=f"tagging {target}")
    _run([runtime, "push", target], what=f"pushing {target}")
    out = _run([runtime, "inspect", "--format={{index .RepoDigests 0}}", target],
               what=f"reading digest for {target}").strip()
    if "@" not in out:
        raise PublishError(f"could not read a digest for {target}: {out!r}")
    return out.rsplit("@", 1)[1]


def _promote_ext4(artifact: "Artifact", to_version: str, token: str) -> str:
    """Re-upload a published rootfs, downloaded through the verified path, at
    the new version and return its digest."""
    from . import image_registry
    from .cli.settings import settings_from_env
    from .paths import host_artifact_cache_dir

    cache_root = host_artifact_cache_dir(settings_from_env().infra.artifact_cache)
    gz = image_registry.ensure_ext4(artifact, cache_root=cache_root)
    url = ext4_url(artifact.image, to_version, artifact.platform)
    print(f"  promoting {artifact.image} {artifact.platform} rootfs -> {to_version}")
    put_generic(url, gz, token)
    return f"sha256:{_sha256(gz)}"


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
        "--promote", type=Path, default=None, metavar="MANIFEST",
        help="promote the artifacts in MANIFEST to --version by copying them; "
             "never rebuilds, so what ships is what was tested. Without "
             "--version, promotes to the first unpublished version at or above "
             "pyproject.toml's",
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
    """Default build concurrency: a quarter of the cores. Each build mostly
    uses one core."""
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
    """Build every (image, platform) pair, up to `jobs` at a time. Failures are
    collected and raised together."""
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
            # Broad so one failure can't swallow the others' results.
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
    # Line-buffer so output interleaves correctly with stderr.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    # Check the token first: an OCI push can't be undone.
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

    if args.promote:
        if args.version is None:
            version = next_release_version(version, token)
        elif existing := already_published(
                list(image_inputs.IMAGES), _RELEASE_PLATFORMS, version, token):
            # `docker tag` + push overwrites silently.
            print(
                f"{len(existing)} artifact(s) already published at {version}; "
                f"pick a free version or omit --version",
                file=sys.stderr,
            )
            return 2
        print(f"promoting to {version}")
        promoted = promote(args.promote, version, token, runtime=args.runtime)
        dest = args.work_dir / ARTIFACTS_MANIFEST_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_artifacts_manifest(promoted, dest)
        link_to_repo([e.name for e in promoted], token)
        return 0
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

    link_to_repo(images, token)

    dest = work_dir / ARTIFACTS_MANIFEST_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_artifacts_manifest(built, dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
