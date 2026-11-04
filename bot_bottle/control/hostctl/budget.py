"""The bottles' budget (`bot-bottle host budget`): what bot-bottle may use on
this host, measured, reported to the console and enforced at launch.

Memory is allocation: every running bottle is given its size
(`BOTTLE_MEMORY_BYTES` unless its `resources:` say otherwise), so what's
allocated is the sum over the bottles running (held, adopted or launching),
whatever they happen to use. A launch that wouldn't fit is refused before it
boots.

Storage is bot-bottle's own directories (the state root and the caches),
walked in the background every few minutes by `StorageMeter`, never on a
launch's path. At 90% of the budget a launch warns; over it, a launch is
refused. Disk that Docker or Apple's `container` keep in their own stores
isn't counted.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from ...backend.bottle_size import BOTTLE_MEMORY_BYTES
from ...lib import log
from ...lib.config_store import ConfigStore, HostBudget

GIB = 1024 ** 3
# How often the storage walk runs.
STORAGE_SECONDS = 300.0
# A launch past this share of the storage budget warns.
STORAGE_WARN_RATIO = 0.9

VM_DISKS, IMAGES, WORKSPACES, TRANSCRIPTS = "vm_disks", "images", "workspaces", "transcripts"
CATEGORIES = (VM_DISKS, IMAGES, WORKSPACES, TRANSCRIPTS)

# What a storage root holds: bottles' state, or re-creatable caches.
STATE, CACHE = "state", "cache"
_STATE_DIRS = {"transcripts": TRANSCRIPTS, "audit": TRANSCRIPTS, "logs": TRANSCRIPTS,
               "build-root": IMAGES, "venv": IMAGES}
# The infra VMs' own disks, under the firecracker cache's `infra/`.
_INFRA_VMS = {"gateway", "orchestrator"}


def host_budget() -> HostBudget:
    """The budget set with `bot-bottle host budget` (the host DB), read fresh
    so a change shows without restarting the host controller."""
    # pylint: disable-next=import-outside-toplevel
    from ...paths import host_db_path
    return ConfigStore(host_db_path()).host_budget()


def gb(n: int) -> str:
    """Bytes as GB for a message: `2`, `1.5`."""
    return f"{n / GIB:.1f}".removesuffix(".0")


# --- storage ---


def _dir_category(kind: str, parts: tuple[str, ...]) -> str | None:
    """The category of everything under the directory at `parts` (relative
    to a root of `kind`), or None while it's still mixed."""
    if kind == STATE:
        return _STATE_DIRS.get(parts[0]) if len(parts) == 1 else None
    name = parts[-1]
    if name == "run" or (name in _INFRA_VMS and len(parts) > 1 and parts[-2] == "infra"):
        return VM_DISKS  # bottles' and infra VMs' disks
    return None


def _file_category(kind: str, name: str) -> str:
    """The category of a file no directory above it decided."""
    if kind == STATE:
        # A kept home disk (firecracker's `home.ext4`) sits in its state dir.
        return VM_DISKS if name.endswith(".ext4") else WORKSPACES
    return IMAGES


def walk(roots: Sequence[tuple[str, Path]]) -> dict[str, int]:
    """Bytes on disk under `roots` by category: allocated blocks, so a sparse
    VM disk counts what it holds, not its size. A root nested in another is
    counted once, and so is a hard-linked file."""
    totals: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
    skip = {str(path) for _, path in roots}
    seen: set[tuple[int, int]] = set()

    def visit(kind: str, directory: str, parts: tuple[str, ...], category: str | None) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry.path in skip:
                    continue
                sub = (*parts, entry.name)
                visit(kind, entry.path, sub, category or _dir_category(kind, sub))
            elif stat.S_ISREG(st.st_mode):
                if st.st_nlink > 1:
                    if (st.st_dev, st.st_ino) in seen:
                        continue
                    seen.add((st.st_dev, st.st_ino))
                totals[category or _file_category(kind, entry.name)] += st.st_blocks * 512

    for kind, root in roots:
        visit(kind, str(root), (), None)
    return totals


def storage_roots() -> tuple[tuple[str, Path], ...]:
    """bot-bottle's own directories: the state root, and the caches."""
    # pylint: disable-next=import-outside-toplevel
    from ...paths import bot_bottle_root, host_artifact_cache_dir, host_firecracker_cache_path
    return ((STATE, bot_bottle_root()), (CACHE, host_artifact_cache_dir()),
            (CACHE, host_firecracker_cache_path()))


class StorageMeter:
    """Walks bot-bottle's directories every `interval` seconds in its own
    thread and keeps the last result; `current()` never walks."""

    def __init__(self, roots: Callable[[], Sequence[tuple[str, Path]]] = storage_roots,
                 interval: float = STORAGE_SECONDS) -> None:
        self._roots = roots
        self._interval = interval
        self._last: dict[str, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def measure(self) -> dict[str, int]:
        """Walk now and keep the result."""
        self._last = walk(self._roots())
        return self._last

    def current(self) -> dict[str, int] | None:
        """The last walk's bytes by category; None before the first."""
        return self._last

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="hostctl-storage-meter")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.measure()
            except Exception as e:  # pylint: disable=broad-exception-caught
                log.debug(f"storage walk failed: {e}")
            self._stop.wait(self._interval)


METER = StorageMeter()


# --- the budget ---


def allocated(bottles: Iterable[Mapping[str, object]]) -> int:
    return sum(int(str(b.get("memory", 0))) for b in bottles)


def refusal(label: str, bottles: Sequence[Mapping[str, object]], budget: HostBudget,
            storage: Mapping[str, int] | None,
            needs: int = BOTTLE_MEMORY_BYTES) -> str | None:
    """Why a bottle named `label`, given `needs` bytes of memory, can't launch
    now, or None when it fits. `bottles` are the running ones
    (`BottleService.allocation`); `storage` is the last walk (None skips the
    storage check)."""
    if budget.memory_gb:
        limit, used = budget.memory_gb * GIB, allocated(bottles)
        if used + needs > limit:
            return (f"{label} needs {gb(needs)} GB of memory; "
                    f"{gb(max(limit - used, 0))} GB left in the {gb(limit)} GB budget. "
                    "Stop a bottle (`bot-bottle bots`) or raise the budget "
                    "(`bot-bottle host budget --memory …`).")
    if budget.disk_gb and storage is not None:
        limit, used = budget.disk_gb * GIB, sum(storage.values())
        if used > limit:
            return (f"{label} can't launch: bottles use {gb(used)} GB of storage, over the "
                    f"{gb(limit)} GB budget. Free some (`bot-bottle cleanup images`) or "
                    "raise the budget (`bot-bottle host budget --disk …`).")
    return None


def storage_warning(budget: HostBudget, storage: Mapping[str, int] | None) -> str | None:
    """A warning when bottles' storage is at 90% of its budget or more."""
    if not budget.disk_gb or storage is None:
        return None
    limit, used = budget.disk_gb * GIB, sum(storage.values())
    if used < limit * STORAGE_WARN_RATIO:
        return None
    return (f"bottles use {gb(used)} of the {gb(limit)} GB storage budget; "
            "`bot-bottle cleanup images` frees cached images")


def block(bottles: Sequence[Mapping[str, object]], budget: HostBudget,
          storage: Mapping[str, int] | None) -> dict[str, object]:
    """The `budget` block of `host_stats`, in bytes (a budget of 0 = none).
    `storage` is left out until the first walk."""
    out: dict[str, object] = {"memory": {
        "allocated": allocated(bottles), "budget": budget.memory_gb * GIB,
        "bottle_size": BOTTLE_MEMORY_BYTES,
        "bottles": [
            {**{"id": b.get("id", ""), "label": b.get("label", ""),
                "memory": b.get("memory", 0)},
             **({} if b.get("cpu_pct") is None else {"cpu_pct": b["cpu_pct"]}),
             **({} if b.get("mem_used") is None else {"mem_used": b["mem_used"]})}
            for b in bottles
        ],
    }}
    if storage is not None:
        out["storage"] = {"used": sum(storage.values()), "budget": budget.disk_gb * GIB,
                          "categories": {c: storage.get(c, 0) for c in CATEGORIES}}
    return out


__all__ = ["METER", "StorageMeter", "block", "host_budget", "refusal", "storage_roots",
           "storage_warning", "walk"]
