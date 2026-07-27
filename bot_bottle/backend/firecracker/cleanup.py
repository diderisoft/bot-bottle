"""Cleanup for the Firecracker backend.

Reaps *orphans* only — resources with no live VM behind them:

  * orphan run dirs: a per-bottle run dir (holding the ~1G rootfs.ext4)
    whose firecracker process has exited. These leak when a launch is
    hard-killed before its teardown runs (host OOM/crash, a cancelled CI
    job, `kill -9`); the clean-exit path already removes its own dir in
    launch.py.
  * orphan VM pids: a firecracker process whose run dir is already gone
    — a VMM left lingering after its dir was removed.

A run dir with a *live* firecracker process is a running bottle and is
left strictly alone: it is neither killed nor removed. Active-agent
enumeration uses this same process snapshot, so cleanup and generic
backend consumers agree about which bottles are running.

TAP slots free themselves (the flock drops when the launcher exits), so
there is nothing to reclaim there.
"""

from __future__ import annotations

from collections.abc import Sequence
import os
import shutil
import signal
import subprocess
from pathlib import Path

from ...log import info
from .. import EnumerationError
from . import lifecycle_lock, util
from .bottle_cleanup_plan import FirecrackerBottleCleanupPlan


def _run_root() -> Path:
    return util.cache_dir() / "run"


def _run_dir_of(args: Sequence[str], run_root: Path) -> Path | None:
    """The bottle run dir a firecracker cmdline belongs to, or None.

    A bottle VM is launched with `--config-file <run_root>/<slug>/config.json`,
    so the run dir is the config file's parent when it sits directly under
    the run root. Anything else (a builder VM, the infra VM elsewhere) is
    not ours to reap here.
    """
    for i, arg in enumerate(args):
        if arg == "--config-file" and i + 1 < len(args):
            parent = Path(args[i + 1]).parent
            if parent.parent == run_root:
                return parent
    return None


def _decode_cmdline(raw: bytes) -> tuple[str, ...]:
    """Decode Linux's NUL-delimited argv without losing embedded spaces."""
    return tuple(
        value.decode(errors="surrogateescape")
        for value in raw.split(b"\0") if value
    )


def _process_args(pid: int) -> tuple[str, ...] | None:
    """Read one process's lossless argv, or None when it exited meanwhile."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EnumerationError(
            f"could not inspect Firecracker pid {pid}: {exc}"
        ) from exc
    return _decode_cmdline(raw)


def _scan_processes(run_root: Path) -> tuple[set[str], list[int]]:
    """Inspect running firecracker VMs under ``run_root``.

    Returns ``(live_run_dirs, orphan_pids)``:
      * ``live_run_dirs`` — run dirs backed by a running VM (never reaped);
      * ``orphan_pids`` — firecracker pids whose run dir no longer exists
        (a lingering VMM to kill).
    """
    try:
        result = subprocess.run(
            ["pgrep", "firecracker"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise EnumerationError(
            f"could not enumerate Firecracker processes: {exc}"
        ) from exc
    if result.returncode == 1:
        # pgrep's documented "no processes matched" result.
        return set(), []
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise EnumerationError(
            f"could not enumerate Firecracker processes: {detail}"
        )
    live: set[str] = set()
    orphan_pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        args = _process_args(pid)
        if args is None:
            continue
        run_dir = _run_dir_of(args, run_root)
        if run_dir is None:
            continue
        if run_dir.is_dir():
            live.add(str(run_dir))
        else:
            orphan_pids.append(pid)
    return live, orphan_pids


def live_run_dirs() -> tuple[Path, ...]:
    """Run directories backed by currently running agent microVMs."""
    live, _ = _scan_processes(_run_root())
    return tuple(Path(path) for path in sorted(live))


def _orphan_run_dirs(run_root: Path, live: set[str]) -> list[str]:
    """Run dirs with no live VM behind them — the leaked ones to remove."""
    if not run_root.is_dir():
        return []
    return sorted(
        str(p) for p in run_root.iterdir()
        if p.is_dir() and str(p) not in live
    )


def prepare_cleanup() -> FirecrackerBottleCleanupPlan:
    run_root = _run_root()
    live, orphan_pids = _scan_processes(run_root)
    return FirecrackerBottleCleanupPlan(
        vm_pids=tuple(orphan_pids),
        run_dirs=tuple(_orphan_run_dirs(run_root, live)),
    )


def cleanup(plan: FirecrackerBottleCleanupPlan) -> None:
    """Revalidate the preview under the launch lock, then remove its survivors."""
    with lifecycle_lock.hold():
        fresh = prepare_cleanup()
        approved_pids = set(plan.vm_pids).intersection(fresh.vm_pids)
        approved_dirs = set(plan.run_dirs).intersection(fresh.run_dirs)
        for pid in sorted(approved_pids):
            _terminate_orphan(pid, _run_root())
        for path in sorted(approved_dirs):
            info(f"rm -rf {path}")
            shutil.rmtree(path, ignore_errors=True)


def _terminate_orphan(pid: int, run_root: Path) -> None:
    """Signal exactly the process identity that still owns an orphan config."""
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise EnumerationError(
            f"could not pin Firecracker pid {pid} for cleanup: {exc}"
        ) from exc
    try:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise EnumerationError(
                f"could not revalidate Firecracker pid {pid}: {exc}"
            ) from exc
        args = _decode_cmdline(raw)
        run_dir = _run_dir_of(args, run_root)
        if run_dir is None or run_dir.is_dir():
            return
        info(f"kill firecracker VM pid {pid}")
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    finally:
        os.close(pidfd)
