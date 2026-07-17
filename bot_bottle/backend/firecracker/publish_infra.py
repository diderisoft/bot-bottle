"""Build the infra rootfs and publish it as a Gitea generic package.

The off-host (build / CI) half of PRD 0069 Stage 2: this DOES use Docker, but
never on the launch host. It runs the same pipeline the launch host used to run
locally — `docker build` the three fixed images, export to a rootfs dir, inject
the guest boot, `mke2fs` to an ext4 with the buildah build slack — then gzips
the ext4 and PUTs it (plus a `.sha256`) to
`…/api/packages/<owner>/generic/bot-bottle-infra/<version>/`.

The `<version>` is `infra_artifact.infra_artifact_version(...)`, the content
hash of the rootfs inputs, so a launch host at the same code checkout resolves
the exact artifact this produced.

  python3 -m bot_bottle.backend.firecracker.publish_infra [--dry-run] [--force]

Auth: a token with `write:package` on the target owner, from
`BOT_BOTTLE_INFRA_ARTIFACT_TOKEN`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import infra_artifact, infra_vm, util

_CHUNK = 1 << 20


def _gzip(src: Path, dest: Path) -> None:
    with open(src, "rb") as fh, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(fh, out, _CHUNK)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _put(url: str, body: bytes, token: str) -> None:
    req = urllib.request.Request(url, data=body, method="PUT")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="publish_infra", description="Build + publish the infra rootfs artifact.")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the artifact but do not upload")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an already-published artifact of this version")
    args = parser.parse_args(argv)

    _, _, token = infra_artifact._config()
    if not args.dry_run and not token:
        raise SystemExit(
            "no publish token: set BOT_BOTTLE_INFRA_ARTIFACT_TOKEN to a token "
            "with write:package")

    with tempfile.TemporaryDirectory(prefix="bb-publish-infra.") as tmp:
        version, gz, sha = build_artifact(Path(tmp))
        gz_url = infra_artifact.artifact_url(version, gz.name)
        sha_url = infra_artifact.artifact_url(version, sha.name)
        if args.dry_run:
            print(f"dry-run: would upload -> {gz_url}")
            return 0
        if args.force:
            _delete(gz_url, token)
            _delete(sha_url, token)
        _put(gz_url, gz.read_bytes(), token)
        _put(sha_url, sha.read_bytes(), token)
    print(f"published infra rootfs {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
