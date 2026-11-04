"""`bot-bottle host`: settings for this host as a whole.

  budget [--memory GB] [--disk GB]   show or set the bottles' budget: memory
                                     allocated to running bottles, and storage
                                     in bot-bottle's own directories (0 clears
                                     one). A launch that wouldn't fit is refused.
  caps                               the old name for `budget` (deprecated)

The host controller reads the budget at each launch and each time it reports
to the console, so a change applies at once, no restart needed.
"""

from __future__ import annotations

import argparse
import sys

from ...backend.bottle_size import BOTTLE_MEMORY_MIB
from ...control.hostctl.budget import host_budget
from ...lib.config_store import ConfigStore, HostBudget
from ..constants import PROG


def _gb(value: int) -> str:
    return f"{value} GB" if value else "none"


def _budget(ns: argparse.Namespace) -> int:
    from ...paths import host_db_path  # pylint: disable=import-outside-toplevel
    if ns.action == "caps":
        sys.stderr.write(f"{PROG} host caps is now `{PROG} host budget`\n")
    store = ConfigStore(host_db_path())
    if ns.disk is not None or ns.memory is not None:
        if any(v is not None and v < 0 for v in (ns.disk, ns.memory)):
            sys.stderr.write(f"{PROG} host budget: GB must be 0 or more (0 clears it)\n")
            return 2
        stored = store.host_budget()
        store.migrate()
        store.set_host_budget(HostBudget(
            disk_gb=stored.disk_gb if ns.disk is None else ns.disk,
            memory_gb=stored.memory_gb if ns.memory is None else ns.memory))
    shown = host_budget()
    sys.stdout.write(f"memory: {_gb(shown.memory_gb)} "
                     f"(each bottle is given {BOTTLE_MEMORY_MIB // 1024} GB unless its resources: say otherwise)\n"
                     f"storage: {_gb(shown.disk_gb)}\n")
    return 0


def _add_budget(sub: "argparse._SubParsersAction[argparse.ArgumentParser]", name: str,
                help_text: str) -> None:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--memory", type=int, metavar="GB",
                        help="memory running bottles may be given, in GB (0 clears it)")
    parser.add_argument("--disk", type=int, metavar="GB",
                        help="storage bot-bottle's directories may use, in GB (0 clears it)")
    parser.set_defaults(run=_budget)


def cmd_host(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} host", description=(__doc__ or "").split("\n", maxsplit=1)[0])
    sub = parser.add_subparsers(dest="action", required=True)
    _add_budget(sub, "budget", "show or set the bottles' memory and storage budget")
    _add_budget(sub, "caps", "deprecated: use `budget`")
    ns = parser.parse_args(argv)
    return ns.run(ns)


__all__ = ["cmd_host"]
