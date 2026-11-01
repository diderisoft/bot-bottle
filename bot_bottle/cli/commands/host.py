"""`bot-bottle host`: settings for this host as a whole.

  caps [--disk GB] [--memory GB]   show or set the advisory storage and memory
                                   caps the console shows this host's usage
                                   against (0 clears one)

The host controller reads the caps each time it reports its usage to the
console, so a change shows within a minute, no restart needed.
"""

from __future__ import annotations

import argparse
import sys

from ...control.hostctl.host_stats import caps as effective_caps
from ...lib.config_store import ConfigStore, HostCaps
from ..constants import PROG


def _gb(value: int) -> str:
    return f"{value} GiB" if value else "none"


def _caps(ns: argparse.Namespace) -> int:
    from ...paths import host_db_path  # pylint: disable=import-outside-toplevel
    store = ConfigStore(host_db_path())
    if ns.disk is not None or ns.memory is not None:
        if any(v is not None and v < 0 for v in (ns.disk, ns.memory)):
            sys.stderr.write(f"{PROG} host caps: GB must be 0 or more (0 clears the cap)\n")
            return 2
        stored = store.host_caps()
        store.migrate()
        store.set_host_caps(HostCaps(
            disk_gb=stored.disk_gb if ns.disk is None else ns.disk,
            memory_gb=stored.memory_gb if ns.memory is None else ns.memory))
    shown = effective_caps()
    sys.stdout.write(f"storage: {_gb(shown.disk_gb)}\nmemory: {_gb(shown.memory_gb)}\n")
    return 0


def cmd_host(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} host", description=(__doc__ or "").split("\n", maxsplit=1)[0])
    sub = parser.add_subparsers(dest="action", required=True)
    caps = sub.add_parser(
        "caps", help="show or set the advisory storage and memory caps shown in the console")
    caps.add_argument("--disk", type=int, metavar="GB",
                      help="storage cap, in whole GiB (0 clears it)")
    caps.add_argument("--memory", type=int, metavar="GB",
                      help="memory cap, in whole GiB (0 clears it)")
    caps.set_defaults(run=_caps)
    ns = parser.parse_args(argv)
    return ns.run(ns)


__all__ = ["cmd_host"]
