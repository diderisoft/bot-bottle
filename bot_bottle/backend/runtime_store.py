"""What Docker and Apple's `container` keep in their own stores for
bot-bottle, for the storage budget (`control/hostctl/budget.py`).

The storage walk sees only bot-bottle's directories, but on the docker and
macos-container backends most of a bottle's disk is the runtime's: images,
containers' writable layers, volumes. Each runtime on the PATH is asked here,
in the budget's background walk, never on a launch's path:

- images: bot-bottle's (`bot-bottle-*`, any registry), shared by every
  environment like the caches. Docker reports each image's unique bytes and
  the bytes it shares with others; shared layers count once (the largest
  share), so the total is an estimate, not a sum of image sizes.
- vm_disks: this environment's containers (their writable layers) and
  volumes, by the environment's resource prefix.

Docker's build cache isn't labelled by who built it, so it isn't counted. A
runtime that can't be asked (not running, timed out, unreadable output) is
named in `unknown` rather than counted as zero.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..environment import resource_prefix
from .host_resources import IMAGE_PREFIX

# A runtime that doesn't answer within this is reported unknown.
_TIMEOUT_SECONDS = 60.0
_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
# Apple `container`'s own store (`container system` keeps its data here).
_CONTAINER_STORE = Path.home() / "Library" / "Application Support" / "com.apple.container"


@dataclass(frozen=True)
class StoreUsage:
    """Bytes a runtime holds for bot-bottle, by budget category, and the
    runtimes that couldn't be measured (`docker: <why>`)."""

    images: int = 0
    vm_disks: int = 0
    unknown: tuple[str, ...] = field(default_factory=tuple)

    def __add__(self, other: StoreUsage) -> StoreUsage:
        return StoreUsage(self.images + other.images, self.vm_disks + other.vm_disks,
                          self.unknown + other.unknown)


def parse_size(value: str) -> int:
    """A size as Docker prints it (`1.2GB`, `144kB`, `12kB (virtual 1GB)`)."""
    first = value.split(" ", 1)[0].strip()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if first.endswith(unit):
            try:
                return int(float(first[: -len(unit)]) * _UNITS[unit])
            except ValueError:
                return 0
    return 0


def _ours(repository: str) -> bool:
    return repository.rsplit("/", 1)[-1].startswith(IMAGE_PREFIX)


def _rows(data: dict[str, object], key: str) -> list[dict[str, object]]:
    rows = data.get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def docker_usage_from(data: dict[str, object], prefix: str) -> StoreUsage:
    """bot-bottle's share of `docker system df -v --format '{{json .}}'`."""
    unique, shared, seen = 0, 0, set()
    for image in _rows(data, "Images"):
        ident = str(image.get("ID", ""))
        if not _ours(str(image.get("Repository", ""))) or ident in seen:
            continue
        seen.add(ident)
        unique += parse_size(str(image.get("UniqueSize", "")))
        shared = max(shared, parse_size(str(image.get("SharedSize", ""))))
    disks = sum(parse_size(str(c.get("Size", ""))) for c in _rows(data, "Containers")
                if str(c.get("Names", "")).startswith(prefix))
    disks += sum(parse_size(str(v.get("Size", ""))) for v in _rows(data, "Volumes")
                 if str(v.get("Name", "")).startswith(prefix))
    return StoreUsage(images=unique + shared, vm_disks=disks)


def _run(argv: Sequence[str]) -> str:
    """`argv`'s output; raises RuntimeError with a short reason."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True, check=False,
                             timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"no answer in {int(_TIMEOUT_SECONDS)}s") from e
    except OSError as e:
        raise RuntimeError(str(e)) from e
    if out.returncode != 0:
        lines = out.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"exit {out.returncode}")
    return out.stdout


def docker_usage(prefix: str, run: Callable[[Sequence[str]], str] = _run) -> StoreUsage:
    try:
        data = json.loads(run(["docker", "system", "df", "-v", "--format", "{{json .}}"]))
    except (RuntimeError, ValueError) as e:
        return StoreUsage(unknown=(f"docker: {e}",))
    if not isinstance(data, dict):
        return StoreUsage(unknown=("docker: unexpected `system df` output",))
    return docker_usage_from(data, prefix)


def _allocated(path: Path) -> int:
    """Allocated bytes under `path` (sparse disks count what they hold)."""
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_blocks * 512
    return total


def container_images_from(images: list[dict[str, object]]) -> int:
    """bot-bottle's images in `container image list --format json`, each
    variant (platform) once by digest."""
    total, seen = 0, set()
    for entry in images:
        config = entry.get("configuration")
        if not isinstance(config, dict) or not _ours(str(config.get("name", ""))):
            continue
        variants = entry.get("variants")
        for variant in variants if isinstance(variants, list) else []:
            if not isinstance(variant, dict):
                continue
            key = str(variant.get("digest") or id(variant))
            if key not in seen:
                seen.add(key)
                total += int(variant.get("size") or 0)
    return total


def container_disks_from(store: Path, prefix: str) -> int:
    """This environment's containers and volumes in Apple `container`'s store:
    one directory each, named after the container or volume."""
    total = 0
    for kind in ("containers", "volumes"):
        try:
            entries = list(os.scandir(store / kind))
        except OSError:
            continue
        total += sum(_allocated(Path(e.path)) for e in entries
                     if e.name.startswith(prefix) and e.is_dir(follow_symlinks=False))
    return total


def container_usage(prefix: str, run: Callable[[Sequence[str]], str] = _run,
                    store: Path = _CONTAINER_STORE) -> StoreUsage:
    try:
        data = json.loads(run(["container", "image", "list", "--format", "json"]))
    except (RuntimeError, ValueError) as e:
        return StoreUsage(unknown=(f"container: {e}",))
    images = container_images_from([d for d in data if isinstance(d, dict)]
                                   if isinstance(data, list) else [])
    if not store.is_dir():
        return StoreUsage(images=images,
                          unknown=(f"container: no store at {store} (containers, volumes)",))
    return StoreUsage(images=images, vm_disks=container_disks_from(store, prefix))


def runtime_usage(which: Callable[[str], str | None] = shutil.which) -> StoreUsage:
    """What every runtime on the PATH holds for bot-bottle."""
    prefix = resource_prefix()
    usage = StoreUsage()
    if which("docker"):
        usage += docker_usage(prefix)
    if which("container"):
        usage += container_usage(prefix)
    return usage


__all__ = ["StoreUsage", "container_usage", "docker_usage", "docker_usage_from",
           "parse_size", "runtime_usage"]
