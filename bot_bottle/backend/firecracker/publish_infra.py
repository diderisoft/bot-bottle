"""Build the infra rootfs and publish it as a Gitea generic package.

The off-host (build / CI) half of PRD 0069 Stage 2: this DOES use Docker, but
never on the launch host. It runs the same pipeline the launch host used to run
locally — `docker build` the three fixed images, export to a rootfs dir, inject
the guest boot, `mke2fs` to an ext4 with the buildah build slack — then gzips
the ext4 and PUTs it (plus a `.sha256`) to
`…/api/packages/<owner>/generic/bot-bottle-firecracker-infra/<version>/`.

The `<version>` is `infra_artifact.infra_artifact_version(...)`, the content
hash of the rootfs inputs, so a launch host at the same code checkout resolves
the exact artifact this produced.

  python3 -m bot_bottle.backend.firecracker.publish_infra --output DIR
  python3 -m bot_bottle.backend.firecracker.publish_infra --publish-dir DIR

Auth: a token with `write:package` on the target owner, from
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

# A human-readable description shipped alongside the artifact — generic packages
# have no description field, so this file *is* the description on the package
# page. Uploaded on every publish so it never goes stale.
_ABOUT_NAME = "about.txt"
_ABOUT_TEXT = (
    "bot-bottle infra rootfs for the Firecracker backend (PRD 0069 Stage 2, "
    "#348): the per-host infra VM (orchestrator control plane + gateway + "
    "buildah). Prebuilt off-host, gzip ext4; the launch host downloads + "
    "sha256-verifies + boots it, no host Docker. The version tag is a content "
    "hash of the rootfs inputs. Files: rootfs.ext4.gz + rootfs.ext4.gz.sha256.\n"
)


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


def build_artifact(out_dir: Path) -> tuple[str, Path, Path]:
    """Build the infra rootfs ext4, gzip it, and write the checksum. Returns
    `(version, gz_path, sha_path)`. Uses host Docker (off-host / CI)."""
    version = infra_artifact.infra_artifact_version(infra_vm._infra_init())
    print(f"building infra rootfs artifact {version} (docker)")
    infra_vm.build_infra_images_with_docker()
    base = infra_vm.build_infra_rootfs_dir()

    ext4 = out_dir / "rootfs.ext4"
    util.build_rootfs_ext4(base, ext4, slack_mib=8192)
    gz = out_dir / "rootfs.ext4.gz"
    print("compressing rootfs")
    _gzip(ext4, gz)
    ext4.unlink(missing_ok=True)

    sha = out_dir / "rootfs.ext4.gz.sha256"
    digest = _sha256(gz)
    sha.write_text(f"{digest}  rootfs.ext4.gz\n")
    print(f"  {gz.name}: {gz.stat().st_size / 1e6:.0f} MB  sha256={digest}")
    return version, gz, sha


def _publish_bundle(root: Path, token: str) -> str:
    version = (root / "version.txt").read_text(encoding="utf-8").strip()
    expected = infra_artifact.infra_artifact_version(infra_vm._infra_init())
    if version != expected:
        raise SystemExit(
            f"artifact bundle version {version!r} does not match checkout {expected!r}"
        )
    gz = root / "rootfs.ext4.gz"
    sha = root / "rootfs.ext4.gz.sha256"
    if not gz.is_file() or not sha.is_file():
        raise SystemExit(f"incomplete artifact bundle: {root}")
    expected_sha = sha.read_text().split()[0].strip().lower()
    if _sha256(gz) != expected_sha:
        raise SystemExit("artifact bundle checksum mismatch")

    gz_url = infra_artifact.artifact_url(version, gz.name)
    sha_url = infra_artifact.artifact_url(version, sha.name)
    about_url = infra_artifact.artifact_url(version, _ABOUT_NAME)

    # Publishing is idempotent. If this exact complete artifact is already
    # present, a test-only main commit is a no-op. Otherwise clear any partial
    # upload left by an interrupted prior attempt and upload the complete set.
    try:
        with urllib.request.urlopen(infra_artifact._open(sha_url)) as resp:
            remote_sha = resp.read().decode("utf-8").split()[0].strip().lower()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise SystemExit(f"checking existing artifact failed (HTTP {e.code})")
        remote_sha = ""
    except urllib.error.URLError as e:
        raise SystemExit(f"registry unreachable: {sha_url} ({e.reason})")
    if remote_sha == expected_sha:
        print(f"infra rootfs {version} already published")
        return version

    for url in (gz_url, sha_url, about_url):
        _delete(url, token)
    _put(gz_url, gz, token)
    _put(sha_url, sha.read_bytes(), token)
    _put(about_url, _ABOUT_TEXT.encode(), token)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_infra", description="Build + publish the infra rootfs artifact.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path,
                      help="build a candidate bundle in DIR without publishing")
    mode.add_argument("--publish-dir", type=Path,
                      help="publish an already-built and tested candidate bundle")
    args = parser.parse_args(argv)

    _, _, token = infra_artifact._config()
    if args.publish_dir is not None and not token:
        raise SystemExit(
            "no publish token: set BOT_BOTTLE_INFRA_ARTIFACT_TOKEN to a token "
            "with write:package")

    if args.output is not None:
        args.output.mkdir(parents=True, exist_ok=True)
        version, _gz, _sha = build_artifact(args.output)
        (args.output / "version.txt").write_text(version + "\n", encoding="utf-8")
        print(f"built infra rootfs candidate {version}")
        return 0

    version = _publish_bundle(args.publish_dir, token)
    print(f"published infra rootfs {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
