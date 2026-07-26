"""Prebuilt infra-VM rootfs images, pulled as artifacts (PRD 0069 Stage 2).

The two Firecracker infra VMs each boot a fixed per-plane rootfs that does not
vary per launch — the per-boot bits (authorized_keys, guest IP) ride the kernel
cmdline, so one rootfs boots on any host:

  * `orchestrator` — the control plane + buildah (in-VM agent-image builds);
  * `gateway` — the slim data plane (no build tooling on the exposed VM).

Instead of building them on the launch host with Docker, we build them
**off-host** and publish each as a versioned, ready-to-boot ext4 (gzip-
compressed) to a per-role Gitea **generic package**; the launch host downloads +
verifies + boots them. No Docker, no image tooling on the launch host — just an
HTTP fetch and gunzip.

  publish (off-host, see publish_infra.py):
    docker build -> rootfs dir -> mke2fs -> gzip -> PUT generic package
  pull (this module, launch host):
    GET .../rootfs.ext4.gz (+ .sha256) -> verify -> gunzip -> boot

Each artifact **version** is a content hash of everything baked into that rootfs
(the shipped bot_bottle package, the role's Dockerfiles, and the role init), so
a launch host always pulls the artifact matching its code and a content change
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

from ... import resources
from ...log import die, info
from . import util

# Bump if the on-disk artifact *format* changes (compression, layout) so a new
# scheme can't collide with a cached/published artifact of the old one.
_ARTIFACT_FORMAT = "1"

# The two per-plane infra VM roles. Each publishes/pulls its own rootfs artifact
# from its own generic package; the Dockerfiles baked into each differ (only the
# orchestrator rootfs carries buildah), so the versions are hashed separately.
ROLES = ("orchestrator", "gateway")
_BUILD_INPUTS = {
    "orchestrator": (
        "image-build-args.json",
        "Dockerfile.orchestrator",
        "Dockerfile.orchestrator.fc",
        "requirements.orchestrator.lock",
    ),
    "gateway": (
        "image-build-args.json",
        "Dockerfile.gateway",
        "requirements.gateway.lock",
    ),
}

_DEFAULT_BASE = "https://gitea.dideric.is"
_DEFAULT_OWNER = "didericis"


def _package(role: str) -> str:
    return f"bot-bottle-firecracker-{role}"


# Streaming copy chunk for the (hundreds-of-MB) download.
_CHUNK = 1 << 20


def local_build_requested() -> bool:
    """True when the operator opted into the dev Docker-build path instead of
    pulling the published artifact (`BOT_BOTTLE_INFRA_BUILD=local`)."""
    return os.environ.get("BOT_BOTTLE_INFRA_BUILD", "").strip().lower() == "local"


def infra_artifact_version(
    init_script: str, role: str, *, repo_root: Path | None = None,
) -> str:
    """Content hash (16 hex) of everything baked into `role`'s infra rootfs: the
    whole shipped `bot_bottle` package, that role's Dockerfiles, and its guest
    init. Deterministic across the publish host and the launch host when both run
    the same checkout, so the tag the launch host pulls is exactly the tag
    publish produced.

    The package is baked into both images wholesale (orchestrator `COPY`s it,
    gateway `pip install`s it), so hash *every* regular file under it — not just
    `*.py`. Non-Python inputs (e.g. `gateway/egress/entrypoint.sh`,
    `netpool.defaults.env`) are baked in too, and a change to one must bump the
    version or a launch host could boot a stale rootfs whose code differs from
    its checkout. `__pycache__`/`.pyc` are the only exclusions — build artifacts,
    never copied."""
    if repo_root is None:
        repo_root = resources.build_root()
    h = hashlib.sha256()
    h.update(f"format={_ARTIFACT_FORMAT}\nrole={role}\n".encode())
    pkg = repo_root / "bot_bottle"
    for path in sorted(pkg.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    for name in _BUILD_INPUTS[role]:
        h.update(name.encode())
        h.update(b"\0")
        h.update((repo_root / name).read_bytes())
    h.update(b"pyproject.toml\0")
    h.update((repo_root / "pyproject.toml").read_bytes())
    h.update(b"dropbear\0")
    dropbear = util.dropbear_path()
    h.update(dropbear.read_bytes() if dropbear.is_file() else b"<missing>")
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


def artifact_url(version: str, filename: str, *, role: str) -> str:
    """The generic-package download URL for one file of `role`'s artifact at
    `version` (`rootfs.ext4.gz` / `rootfs.ext4.gz.sha256`)."""
    base, owner, _ = _config()
    return f"{base}/api/packages/{owner}/generic/{_package(role)}/{version}/{filename}"


_GZ_NAME = "rootfs.ext4.gz"
_SHA_NAME = "rootfs.ext4.gz.sha256"
_CANDIDATE_DIR_ENV = "BOT_BOTTLE_INFRA_ARTIFACT_DIR"


def _cache_root(version: str, role: str) -> Path:
    return util.cache_dir() / "infra-artifact" / role / version


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


def ensure_artifact_gz(version: str, *, role: str) -> Path:
    """The verified, cached `rootfs.ext4.gz` for `role` at `version` —
    downloading it (and its `.sha256`) once, then reusing it. Fail-closed on a
    checksum mismatch: the partial is removed and we die rather than boot an
    unverified rootfs.

    A pre-staged candidate bundle (`BOT_BOTTLE_INFRA_ARTIFACT_DIR`) holds each
    role under its own `<dir>/<role>/` subdir."""
    candidate_dir = os.environ.get(_CANDIDATE_DIR_ENV, "").strip()
    if candidate_dir:
        root = Path(candidate_dir) / role
        version_file = root / "version.txt"
        # Guard the read so a missing version.txt is a clean error, not a raw
        # FileNotFoundError.
        if not version_file.is_file():
            die(f"infra candidate bundle is incomplete: {root}")
        declared = version_file.read_text(encoding="utf-8").strip()
        if declared != version:
            die(
                f"infra candidate version mismatch ({role}): expected {version}, "
                f"bundle contains {declared or '<empty>'}"
            )
        gz = root / _GZ_NAME
        sha = root / _SHA_NAME
        if not gz.is_file() or not sha.is_file():
            die(f"infra candidate bundle is incomplete: {root}")
        expected = sha.read_text().split()[0].strip().lower()
        actual = _sha256_file(gz)
        if actual != expected:
            die(
                f"infra candidate checksum mismatch ({role}) for {version}:\n"
                f"    expected {expected}\n    actual   {actual}"
            )
        return gz

    root = _cache_root(version, role)
    root.mkdir(parents=True, exist_ok=True)
    gz = root / _GZ_NAME
    ok = root / ".verified"
    if gz.is_file() and ok.is_file():
        return gz

    info(f"pulling infra rootfs artifact {_package(role)}/{version}")
    _download(artifact_url(version, _GZ_NAME, role=role), gz)
    sha = root / _SHA_NAME
    _download(artifact_url(version, _SHA_NAME, role=role), sha)

    expected = sha.read_text().split()[0].strip().lower()
    actual = _sha256_file(gz)
    if actual != expected:
        gz.unlink(missing_ok=True)
        sha.unlink(missing_ok=True)
        die(
            f"infra artifact checksum mismatch ({role}) for {version}:\n"
            f"    expected {expected}\n"
            f"    actual   {actual}\n"
            f"    refusing to boot an unverified rootfs."
        )
    ok.write_text("ok\n")
    return gz


def materialize_ext4(version: str, dest: Path, *, role: str) -> None:
    """Ensure the verified `role` artifact is cached, then gunzip it to `dest` —
    a fresh, writable per-boot rootfs (the VM mutates it; the cached `.gz` stays
    pristine). Atomic via a `.part` sibling."""
    gz = ensure_artifact_gz(version, role=role)
    tmp = dest.with_suffix(dest.suffix + ".part")
    info(f"expanding {role} infra rootfs -> {dest}")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, _CHUNK)
    tmp.replace(dest)
