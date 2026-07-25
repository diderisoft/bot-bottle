"""Build the infra rootfs artifacts and publish them as Gitea generic packages.

The off-host (build / CI) half of PRD 0069 Stage 2: this DOES use Docker, but
never on the launch host. It runs the same pipeline the launch host used to run
locally — `docker build` the fixed images, export each per-plane rootfs, inject
the guest boot, `mke2fs` to an ext4 — then gzips each and PUTs it (plus a
`.sha256`) to `…/api/packages/<owner>/generic/bot-bottle-firecracker-<role>/<version>/`.

There are two artifacts, one per plane (`orchestrator`, `gateway`); the
orchestrator rootfs carries buildah, the gateway rootfs is slim. Each
`<version>` is `infra_artifact.infra_artifact_version(...)`, the content hash of
that rootfs's inputs, so a launch host at the same code checkout resolves the
exact artifacts this produced.

  python3 -m bot_bottle.backend.firecracker.publish_infra --output DIR
  python3 -m bot_bottle.backend.firecracker.publish_infra --publish-dir DIR

A candidate bundle holds each role under its own `DIR/<role>/` subdir. Auth: a
token with `write:package` on the target owner, from
`BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import infra_artifact, infra_vm, util

_CHUNK = 1 << 20

_GZ_NAME = "rootfs.ext4.gz"
_SHA_NAME = "rootfs.ext4.gz.sha256"

# A human-readable description shipped alongside each artifact — generic packages
# have no description field, so this file *is* the description on the package
# page. Uploaded on every publish so it never goes stale.
_ABOUT_NAME = "about.txt"


def _about_text(role: str) -> str:
    return (
        f"bot-bottle firecracker {role} rootfs (PRD 0069 Stage 2 / PRD 0070): "
        f"the per-host {role} infra VM. Prebuilt off-host, gzip ext4; the launch "
        f"host downloads + sha256-verifies + boots it, no host Docker. The "
        f"version tag is a content hash of the rootfs inputs. Files: "
        f"{_GZ_NAME} + {_SHA_NAME}.\n"
    )


def _role_version(role: str) -> str:
    return infra_artifact.infra_artifact_version(infra_vm.role_init(role), role)


def _gzip(src: Path, dest: Path) -> None:
    with open(src, "rb") as fh, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(fh, out, _CHUNK)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _put(url: str, body: "bytes | Path", token: str) -> None:
    """PUT `body` (raw bytes, or a Path streamed from disk) to `url`. The rootfs
    is hundreds of MB, so it is passed as a Path and streamed — `urlopen` reads
    the open file in blocks rather than materializing it in memory (with an
    explicit Content-Length, which Gitea requires and which also stops urllib
    from `len()`-ing a non-bytes body)."""
    handle = None
    if isinstance(body, Path):
        length = body.stat().st_size
        handle = open(body, "rb")
        data: object = handle
    else:
        length = len(body)
        data = body
    req = urllib.request.Request(url, data=data, method="PUT")  # type: ignore[arg-type]
    req.add_header("Content-Length", str(length))
    if token:
        req.add_header("Authorization", f"token {token}")
    req.add_header("Content-Type", "application/octet-stream")
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"  uploaded {url} (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise SystemExit(
                f"artifact already published at {url} (HTTP 409); "
                f"bump the code version or pass --force to overwrite"
            )
        raise SystemExit(f"upload failed (HTTP {e.code}): {url}\n{e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"registry unreachable: {url} ({e.reason})")
    finally:
        if handle is not None:
            handle.close()


def _delete(url: str, token: str) -> None:
    req = urllib.request.Request(url, method="DELETE")
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urllib.request.urlopen(req):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise SystemExit(f"could not overwrite existing artifact (HTTP {e.code}): {url}")
    except urllib.error.URLError as e:
        raise SystemExit(f"registry unreachable: {url} ({e.reason})")


def build_role_artifact(role: str, role_dir: Path) -> str:
    """Build `role`'s rootfs ext4, gzip it, and write the checksum + version into
    `role_dir`. Returns the version. Assumes the docker images are already
    built (`infra_vm.build_infra_images_with_docker`). Uses host Docker."""
    version = _role_version(role)
    print(f"building {role} rootfs artifact {version} (docker)")
    base = infra_vm.build_rootfs_dir(role)

    ext4 = role_dir / "rootfs.ext4"
    util.build_rootfs_ext4(base, ext4, slack_mib=infra_vm._ROOTFS_SLACK_MIB[role])
    gz = role_dir / _GZ_NAME
    print(f"compressing {role} rootfs")
    _gzip(ext4, gz)
    ext4.unlink(missing_ok=True)

    sha = role_dir / _SHA_NAME
    digest = _sha256(gz)
    sha.write_text(f"{digest}  {_GZ_NAME}\n")
    (role_dir / "version.txt").write_text(version + "\n", encoding="utf-8")
    print(f"  {role}/{gz.name}: {gz.stat().st_size / 1e6:.0f} MB  sha256={digest}")
    return version


def _try_download_published(role: str, role_dir: Path) -> str | None:
    """If `role`'s artifact for this version is already in the registry, download
    the gz + sha into `role_dir` and return the version. None when not yet
    published."""
    version = _role_version(role)
    sha_url = infra_artifact.artifact_url(version, _SHA_NAME, role=role)
    try:
        with urllib.request.urlopen(infra_artifact._open(sha_url)):
            pass
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"registry check failed (HTTP {e.code}): {sha_url}")
    except urllib.error.URLError as e:
        raise SystemExit(f"registry unreachable: {sha_url} ({e.reason})")
    print(f"{role} rootfs {version} already published — downloading instead of building")
    infra_artifact._download(
        infra_artifact.artifact_url(version, _GZ_NAME, role=role), role_dir / _GZ_NAME)
    infra_artifact._download(sha_url, role_dir / _SHA_NAME)
    (role_dir / "version.txt").write_text(version + "\n", encoding="utf-8")
    return version


def _publish_bundle(role: str, role_dir: Path, token: str) -> str:
    version_file = role_dir / "version.txt"
    # Guard the read so a missing version.txt is a clean error, not a raw
    # FileNotFoundError.
    if not version_file.is_file():
        raise SystemExit(f"incomplete {role} artifact bundle: {role_dir}")
    version = version_file.read_text(encoding="utf-8").strip()
    expected = _role_version(role)
    if version != expected:
        raise SystemExit(
            f"{role} artifact bundle version {version!r} does not match checkout {expected!r}"
        )
    gz = role_dir / _GZ_NAME
    sha = role_dir / _SHA_NAME
    if not gz.is_file() or not sha.is_file():
        raise SystemExit(f"incomplete {role} artifact bundle: {role_dir}")
    expected_sha = sha.read_text().split()[0].strip().lower()
    if _sha256(gz) != expected_sha:
        raise SystemExit(f"{role} artifact bundle checksum mismatch")

    gz_url = infra_artifact.artifact_url(version, _GZ_NAME, role=role)
    sha_url = infra_artifact.artifact_url(version, _SHA_NAME, role=role)
    about_url = infra_artifact.artifact_url(version, _ABOUT_NAME, role=role)

    # Publishing is idempotent. If this exact complete artifact is already
    # present, a re-publish is a no-op. Otherwise clear any partial upload left
    # by an interrupted prior attempt and upload the complete set.
    try:
        with urllib.request.urlopen(infra_artifact._open(sha_url)) as resp:
            remote_sha = resp.read().decode("utf-8").split()[0].strip().lower()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise SystemExit(f"checking existing {role} artifact failed (HTTP {e.code})")
        remote_sha = ""
    except urllib.error.URLError as e:
        raise SystemExit(f"registry unreachable: {sha_url} ({e.reason})")
    if remote_sha == expected_sha:
        print(f"{role} rootfs {version} already published")
        return version

    for url in (gz_url, sha_url, about_url):
        _delete(url, token)
    _put(gz_url, gz, token)
    _put(sha_url, sha.read_bytes(), token)
    _put(about_url, _about_text(role).encode(), token)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_infra", description="Build + publish the infra rootfs artifacts.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path,
                      help="build candidate bundles in DIR/<role>/ without publishing")
    mode.add_argument("--publish-dir", type=Path,
                      help="publish already-built + tested candidate bundles under DIR")
    parser.add_argument("--reuse-published", action="store_true",
                        help="with --output: download from registry if already published instead of building")
    args = parser.parse_args(argv)

    _, _, token = infra_artifact._config()
    if args.publish_dir is not None and not token:
        raise SystemExit(
            "no publish token: set BOT_BOTTLE_INFRA_ARTIFACT_TOKEN to a token "
            "with write:package")

    if args.output is not None:
        # Build (or reuse) all roles. Images are built once, up front, only when
        # something actually needs building.
        pending = []
        for role in infra_artifact.ROLES:
            role_dir = args.output / role
            role_dir.mkdir(parents=True, exist_ok=True)
            if args.reuse_published and _try_download_published(role, role_dir):
                print(f"reused published {role} rootfs candidate")
                continue
            pending.append(role)
        if pending:
            print("building infra images (docker)")
            infra_vm.build_infra_images_with_docker()
            for role in pending:
                build_role_artifact(role, args.output / role)
                print(f"built {role} rootfs candidate")
        return 0

    assert args.publish_dir is not None
    for role in infra_artifact.ROLES:
        version = _publish_bundle(role, args.publish_dir / role, token)
        print(f"published {role} rootfs {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
