"""Prebuilt infra-VM rootfs, pulled as an artifact (PRD 0069 Stage 2).

The Firecracker infra VM boots a fixed rootfs (orchestrator control plane +
gateway + buildah, control-plane init as PID 1) that does not vary per launch —
the per-boot bits (authorized_keys, guest IP) ride the kernel cmdline, so one
rootfs boots on any host. Instead of building that rootfs on the launch host
with Docker, we build it **off-host** and publish it as a versioned, ready-to-
boot ext4 (gzip-compressed) to a Gitea **generic package**; the launch host
downloads + verifies + boots it. No Docker, no image tooling on the launch
host — just an HTTP fetch and gunzip.

  publish (off-host, see publish_infra.py):
    docker build -> rootfs dir -> mke2fs -> gzip -> PUT generic package
  pull (this module, launch host):
    GET .../rootfs.ext4.gz (+ .sha256) -> verify -> gunzip -> boot

The artifact **version** is a content hash of everything baked into the rootfs
(the shipped bot_bottle package, the three Dockerfiles, and the init), so a
launch host always pulls the artifact matching its code and a content change
can't silently boot a stale rootfs. A checksum mismatch fails closed.

Set `BOT_BOTTLE_INFRA_BUILD=local` to skip the pull and build the rootfs
locally with Docker (dev iteration on the Dockerfiles) — see `infra_vm`.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from ...log import die, info
from . import util

# Bump if the on-disk artifact *format* changes (compression, layout) so a new
# scheme can't collide with a cached/published artifact of the old one.
_ARTIFACT_FORMAT = "1"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILES = ("Dockerfile.orchestrator", "Dockerfile.gateway", "Dockerfile.infra")

_DEFAULT_BASE = "https://gitea.dideric.is"
_DEFAULT_OWNER = "didericis"
_PACKAGE = "bot-bottle-firecracker-infra"

# Streaming copy chunk for the (hundreds-of-MB) download.
_CHUNK = 1 << 20


def local_build_requested() -> bool:
    """True when the operator opted into the dev Docker-build path instead of
    pulling the published artifact (`BOT_BOTTLE_INFRA_BUILD=local`)."""
    return os.environ.get("BOT_BOTTLE_INFRA_BUILD", "").strip().lower() == "local"


def infra_artifact_version(init_script: str, *, repo_root: Path = _REPO_ROOT) -> str:
    """Content hash (16 hex) of everything baked into the infra rootfs: the
    whole shipped `bot_bottle` package, the three fixed Dockerfiles, and the
    guest init. Deterministic across the publish host and the launch host when
    both run the same checkout, so the tag the launch host pulls is exactly the
    tag publish produced.

    The package is `COPY bot_bottle /app/bot_bottle`'d wholesale into the image,
    so hash *every* regular file under it — not just `*.py`. Non-Python inputs
    (e.g. `egress_entrypoint.sh`, `netpool.defaults.env`) are baked in too, and
    a change to one must bump the version or a launch host could boot a stale
    rootfs whose code differs from its checkout. `__pycache__`/`.pyc` are the
    only exclusions — build artifacts, never copied."""
    h = hashlib.sha256()
    h.update(f"format={_ARTIFACT_FORMAT}\n".encode())
    pkg = repo_root / "bot_bottle"
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    for name in _DOCKERFILES:
        h.update(name.encode())
        h.update(b"\0")
        h.update((repo_root / name).read_bytes())
    h.update(b"pyproject.toml\0")
    h.update((repo_root / "pyproject.toml").read_bytes())
    h.update(b"init\0")
    h.update(init_script.encode())
    return h.hexdigest()[:16]


def _config() -> tuple[str, str, str]:
    """(base_url, owner, token) for the generic-package endpoint. Base + owner
    are overridable for other deployments / mirrors; the token comes solely from
    `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` (a dedicated package-scoped token, kept
    separate from the general-purpose Gitea token) and is optional — a public
    package needs none to pull."""
    base = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_BASE", _DEFAULT_BASE).rstrip("/")
    owner = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_OWNER", _DEFAULT_OWNER)
    token = os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_TOKEN", "")
    return base, owner, token


def artifact_url(version: str, filename: str) -> str:
    """The generic-package download URL for one file of this version's
    artifact (`rootfs.ext4.gz` / `rootfs.ext4.gz.sha256`)."""
    base, owner, _ = _config()
    return f"{base}/api/packages/{owner}/generic/{_PACKAGE}/{version}/{filename}"


_GZ_NAME = "rootfs.ext4.gz"
_SHA_NAME = "rootfs.ext4.gz.sha256"


def _cache_root(version: str) -> Path:
    return util.cache_dir() / "infra-artifact" / version


def _open(url: str) -> urllib.request.Request:
    _, _, token = _config()
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    return req


def _download(url: str, dest: Path) -> None:
    """Stream `url` to `dest` (atomic via a `.part` sibling)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(_open(url)) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out, _CHUNK)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code == 404:
            die(
                f"infra artifact not published for this code version.\n"
                f"    missing: {url}\n"
                f"    publish it from a build host (Docker):\n"
                f"        python3 -m bot_bottle.backend.firecracker.publish_infra\n"
                f"    or build the rootfs locally: BOT_BOTTLE_INFRA_BUILD=local"
            )
        die(f"downloading infra artifact failed (HTTP {e.code}): {url}")
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        die(f"infra artifact registry unreachable: {url} ({e.reason})")
    tmp.replace(dest)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_artifact_gz(version: str) -> Path:
    """The verified, cached `rootfs.ext4.gz` for `version` — downloading it (and
    its `.sha256`) once, then reusing it. Fail-closed on a checksum mismatch:
    the partial is removed and we die rather than boot an unverified rootfs."""
    root = _cache_root(version)
    root.mkdir(parents=True, exist_ok=True)
    gz = root / _GZ_NAME
    ok = root / ".verified"
    if gz.is_file() and ok.is_file():
        return gz

    info(f"pulling infra rootfs artifact {_PACKAGE}/{version}")
    _download(artifact_url(version, _GZ_NAME), gz)
    sha = root / _SHA_NAME
    _download(artifact_url(version, _SHA_NAME), sha)

    expected = sha.read_text().split()[0].strip().lower()
    actual = _sha256_file(gz)
    if actual != expected:
        gz.unlink(missing_ok=True)
        sha.unlink(missing_ok=True)
        die(
            f"infra artifact checksum mismatch for {version}:\n"
            f"    expected {expected}\n"
            f"    actual   {actual}\n"
            f"    refusing to boot an unverified rootfs."
        )
    ok.write_text("ok\n")
    return gz


def materialize_ext4(version: str, dest: Path) -> None:
    """Ensure the verified artifact is cached, then gunzip it to `dest` — a
    fresh, writable per-boot rootfs (the VM mutates it; the cached `.gz` stays
    pristine). Atomic via a `.part` sibling."""
    gz = ensure_artifact_gz(version)
    tmp = dest.with_suffix(dest.suffix + ".part")
    info(f"expanding infra rootfs -> {dest}")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, _CHUNK)
    tmp.replace(dest)
