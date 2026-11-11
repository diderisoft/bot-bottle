"""The `firecracker` binary: when a KVM host has none on PATH, `install`
downloads the pinned upstream release into ~/.local/bin, checking the
tarball's sha256 against the one pinned here.

The pinned version is the one CI's integration suite runs. A `firecracker`
already on PATH (a distro or Nix package, or one installed by hand) is used
as it is.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

VERSION = "v1.16.1"
RELEASES = "https://github.com/firecracker-microvm/firecracker/releases"
# By `platform.machine()`: the release tarball's sha256.
TARBALLS: dict[str, str] = {
    "x86_64": "sha256:382a02a869e4d6d5cb14c40577f9545e8458021ea8b0b2d3fc10ec14d9c242e6",
    "aarch64": "sha256:8d0e69f6d6f9a1724551f607f18504052c16c1828ee3d4d7b6e6c73380871e0e",
}
_TIMEOUT_SECS = 120


class BinaryError(RuntimeError):
    """`firecracker` is missing and couldn't be installed."""


def bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def tarball_url(arch: str) -> str:
    return f"{RELEASES}/download/{VERSION}/firecracker-{VERSION}-{arch}.tgz"


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "bot-bottle"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BinaryError(f"couldn't download {url} ({e})") from e


def ensure(*, say: Callable[[str], None] = lambda _line: None, machine: str | None = None,
           target_dir: Path | None = None,
           download: Callable[[str, Path], None] = _download) -> Path | None:
    """Install the pinned `firecracker` unless one is on PATH; where it went,
    or None when nothing was needed. BinaryError when it can't be."""
    if shutil.which("firecracker") is not None:
        return None
    arch = machine or platform.machine()
    digest = TARBALLS.get(arch)
    if digest is None:
        raise BinaryError(f"no Firecracker release for {arch}; install firecracker yourself "
                          f"({RELEASES})")
    target_dir = target_dir or bin_dir()
    dest = target_dir / "firecracker"
    if dest.is_file():
        return None  # installed already; PATH just doesn't have its directory
    say(f"downloading Firecracker {VERSION} ({arch}) into {target_dir}")
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / "firecracker.tgz"
        download(tarball_url(arch), tgz)
        actual = "sha256:" + hashlib.sha256(tgz.read_bytes()).hexdigest()
        if actual != digest:
            raise BinaryError(f"the Firecracker {VERSION} download hashes to {actual}, "
                              f"not the pinned {digest}")
        member = f"release-{VERSION}-{arch}/firecracker-{VERSION}-{arch}"
        try:
            with tarfile.open(tgz) as tar:
                src = tar.extractfile(member)
                if src is None:
                    raise KeyError(member)
                data = src.read()
        except (KeyError, tarfile.TarError) as e:
            raise BinaryError(f"the Firecracker {VERSION} tarball has no {member}") from e
    target_dir.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    part.write_bytes(data)
    part.chmod(0o755)
    part.replace(dest)
    return dest


__all__ = ["BinaryError", "RELEASES", "TARBALLS", "VERSION", "bin_dir", "ensure", "tarball_url"]
