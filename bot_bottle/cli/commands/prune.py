"""prune: free disk by deleting cached images this install no longer needs.

  bot-bottle prune [--dry-run] [--older-than DAYS] [--build-store]

Safe to run at any time: it never removes a current pin's image, the current
infra build, or an image a container uses. Superseded releases (old pins and
their downloads) go at once; builds (overlays, local and infra builds, base
trees) once unused for DAYS (default 14). `--build-store` also empties
firecracker's buildah store when the orchestrator VM is stopped.

Unlike `cleanup`, which stops and removes leftover bottles and their state,
`prune` only frees disk.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from ...backend import BottleBackend, get_bottle_backend, has_backend, known_backend_names
from ...backend.cache_entries import DEFAULT_OLDER_THAN_DAYS, human, to_remove
from ..constants import PROG
from ..settings import settings_from_env


def _backends() -> list[BottleBackend[Any, Any]]:
    settings = settings_from_env()
    return [get_bottle_backend(settings, name) for name in known_backend_names()
            if has_backend(settings, name)]


def cmd_prune(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} prune", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list what would go; remove nothing")
    parser.add_argument("--older-than", type=float, default=DEFAULT_OLDER_THAN_DAYS,
                        metavar="DAYS", help="how long a build must go unused (default 14)")
    parser.add_argument("--build-store", action="store_true",
                        help="also empty firecracker's buildah store (orchestrator VM stopped)")
    ns = parser.parse_args(argv)

    freed, failed = 0, []
    for backend in _backends():
        try:
            entries = backend.cache_entries()
        except Exception as e:  # pylint: disable=broad-exception-caught  # the rest still prune
            sys.stderr.write(f"{PROG}: {backend.name}: couldn't list cached images: {e}\n")
            continue
        going = to_remove(entries, older_than_days=ns.older_than, now=time.time())
        for entry in going:
            if ns.dry_run or entry.remove():
                freed += entry.size
                print(f"{backend.name}: {'would remove' if ns.dry_run else 'removed'} "
                      f"{entry.what} ({human(entry.size)})")
            else:
                failed.append(entry.what)
        kept = len(entries) - len(going)
        if entries:
            print(f"{backend.name}: kept {kept} of {len(entries)} cached images")
        if ns.build_store:
            store = backend.prune_build_store(dry_run=ns.dry_run)
            if store is not None:
                freed += store[0]
                print(f"{backend.name}: {store[1]}")
    verb = "would free" if ns.dry_run else "freed"
    print(f"{verb} {human(freed)}" + (f"; couldn't remove {len(failed)}" if failed else ""))
    for what in failed:
        print(f"  couldn't remove {what}")
    return 1 if failed else 0


__all__ = ["cmd_prune"]
