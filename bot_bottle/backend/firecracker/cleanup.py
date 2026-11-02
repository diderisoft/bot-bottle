"""Firecracker cleanup reaps orphans only: run dirs whose VM has exited (left
by a hard-killed launch) and VMM pids whose run dir is gone. A run dir with a
live VM is a running bottle and is never touched. TAP slots free themselves
when the launcher exits.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
import select
import signal
from pathlib import Path

from ...contract.hostctl import Settings
from ...lib.log import info
from .. import EnumerationError
from ..cleanup_control import CleanupError, CleanupFailures
from . import lifecycle_lock, util
from .bottle_cleanup_plan import FirecrackerBottleCleanupPlan


def _run_root() -> Path:
    return util.env_dir() / "run"


def _run_dir_of(args: Sequence[str], run_root: Path) -> Path | None:
    """The bottle run dir a firecracker cmdline belongs to (the
    `--config-file`'s parent, directly under the run root), or None.
    """
    for i, arg in enumerate(args):
        if arg == "--config-file" and i + 1 < len(args):
            parent = Path(args[i + 1]).parent
            if parent.parent == run_root:
                return parent
    return None


def _decode_cmdline(raw: bytes) -> tuple[str, ...]:
    """Decode NUL-delimited argv, keeping embedded spaces."""
    return tuple(
        value.decode(errors="surrogateescape")
        for value in raw.split(b"\0") if value
    )


def _process_args(pid: int) -> tuple[str, ...] | None:
    """One process's argv, or None if it exited."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EnumerationError(
            f"could not inspect Firecracker pid {pid}: {exc}"
        ) from exc
    return _decode_cmdline(raw)


def _firecracker_pids(proc_root: Path = Path("/proc")) -> list[int]:
    """Pids of running processes named like `firecracker`, from `/proc`. An
    unreadable `/proc` raises rather than hide live VMs."""
    try:
        entries = [e for e in proc_root.iterdir() if e.name.isdigit()]
    except OSError as exc:
        raise EnumerationError(
            f"could not enumerate Firecracker processes: {exc}"
        ) from exc
    pids: list[int] = []
    for entry in entries:
        try:
            name = (entry / "comm").read_text(errors="replace").strip()
        except OSError:  # exited, or not ours to read
            continue
        if "firecracker" in name:
            pids.append(int(entry.name))
    return sorted(pids)


def _scan_processes(run_root: Path) -> tuple[set[str], list[int]]:
    """Scan firecracker VMs under `run_root`. Returns `(live_run_dirs,
    orphan_pids)`: run dirs with a running VM, and pids whose run dir is gone.
    """
    live: set[str] = set()
    orphan_pids: list[int] = []
    for pid in _firecracker_pids():
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


def stop_vmms(run_dir: Path, *, timeout: float = 5.0) -> None:
    """Stop the VMMs booted from `run_dir` (SIGTERM, then SIGKILL), for a
    bottle whose launching host controller is gone."""
    for pid in _firecracker_pids():
        args = _process_args(pid)
        if args is None or _run_dir_of(args, run_dir.parent) != run_dir:
            continue
        try:
            pidfd = os.pidfd_open(pid)
        except OSError:
            continue
        try:
            info(f"stopping firecracker VM pid {pid}")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    signal.pidfd_send_signal(pidfd, sig)
                except ProcessLookupError:
                    break
                ready, _, _ = select.select([pidfd], [], [], timeout)
                if ready:  # the pidfd turns readable when the process exits
                    break
        finally:
            os.close(pidfd)


def live_run_dirs() -> tuple[Path, ...]:
    """Run dirs backed by running agent VMs."""
    live, _ = _scan_processes(_run_root())
    return tuple(Path(path) for path in sorted(live))


def pid_for_slug(slug: str) -> int | None:
    """PID of the running Firecracker VM process for `slug`, or None."""
    run_root = _run_root()
    run_dir = run_root / slug
    if not run_dir.is_dir():
        return None
    for pid in _firecracker_pids():
        args = _process_args(pid)
        if args is not None and _run_dir_of(args, run_root) == run_dir:
            return pid
    return None


def vcpus_for_slug(slug: str) -> int:
    """How many vCPUs the VM for `slug` was booted with (its run dir's
    `config.json`), or 1 when that can't be read."""
    try:
        config = json.loads((_run_root() / slug / "config.json").read_text())
        count = config["machine-config"]["vcpu_count"]
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    # pylint: disable-next=unidiomatic-typecheck  # bool is an int
    return count if type(count) is int and count > 0 else 1


def _orphan_run_dirs(run_root: Path, live: set[str]) -> list[str]:
    """Run dirs with no live VM."""
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


def cleanup(plan: FirecrackerBottleCleanupPlan, settings: Settings) -> None:
    """Revalidate the plan under the launch lock, then remove what's still
    orphaned."""
    with lifecycle_lock.hold():
        fresh = prepare_cleanup()
        approved_pids = set(plan.vm_pids).intersection(fresh.vm_pids)
        approved_dirs = set(plan.run_dirs).intersection(fresh.run_dirs)
        failures = CleanupFailures(timeout=settings.operation.cleanup_command_timeout_seconds)
        for pid in sorted(approved_pids):
            try:
                _terminate_orphan(pid, _run_root())
            except CleanupError as exc:
                failures.record(str(exc))
        for path in sorted(approved_dirs):
            info(f"rm -rf {path}")
            failures.remove_tree(Path(path), f"removing Firecracker run dir {path}")
        failures.raise_if_any()


def _terminate_orphan(pid: int, run_root: Path) -> None:
    """Signal exactly the process that still owns an orphan config."""
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
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise CleanupError(
                f"could not signal Firecracker pid {pid}: {exc}"
            ) from exc
    finally:
        os.close(pidfd)
