"""cleanup workspaces: remove Firecracker's per-project workspace disks.

  bot-bottle cleanup workspaces [--dry-run] [--all]

Lists every workspace disk (`BOT_BOTTLE_FC_WORKSPACE_DISK`) and removes those
whose project directory is gone. `--all` removes every disk not in use; the
next run of a project copies it in afresh. A disk a running bottle has
attached is never removed.
"""

from __future__ import annotations

import argparse

from ...backend import get_bottle_backend, has_backend, known_backend_names
from ...backend.cache_entries import human
from ...lib.log import info
from ..constants import PROG
from ..settings import settings_from_env


def cmd_cleanup_workspaces(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} cleanup workspaces", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list what would go; remove nothing")
    parser.add_argument("--all", action="store_true",
                        help="remove every disk not in use, not just those of removed projects")
    args = parser.parse_args(argv)
    settings = settings_from_env()
    backends = [get_bottle_backend(settings, name) for name in known_backend_names()
                if has_backend(settings, name)]
    disks = [(backend, disk) for backend in backends for disk in backend.workspace_disks()]
    if not disks:
        info("no workspace disks")
        return 0
    freed = 0
    for backend, disk in disks:
        state = "in use" if disk.in_use else "project gone" if disk.orphaned else "kept"
        info(f"{human(disk.bytes):>8}  {state:<12}  {disk.project or '?'}")
        if disk.in_use or not (args.all or disk.orphaned):
            continue
        if args.dry_run or backend.remove_workspace_disk(disk.path):
            freed += disk.bytes
    verb = "would free" if args.dry_run else "freed"
    info(f"{verb} {human(freed)}")
    return 0
