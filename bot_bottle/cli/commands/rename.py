"""rename: give a bottled agent a new label, the name `bot-bottle bots` and
the console show for it. Its identity, and everything keyed by it, stays.

    bot-bottle rename <bottled agent> <label>
"""

from __future__ import annotations

import argparse

from ...control.hostctl.relabel import rename
from ...control.hostctl.sessions import SessionManager
from ...lib.log import die, info
from ..constants import PROG


def cmd_rename(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} rename", add_help=True)
    parser.add_argument("identity", metavar="bottled-agent",
                        help=f"the bottled agent to rename (`{PROG} bots` lists them)")
    parser.add_argument("label", help="its new label")
    args = parser.parse_args(argv)
    result = rename(SessionManager({}), args.identity, args.label)
    if "error" in result:
        die(str(result["error"]))
    info(f"{args.identity} is now {result['label']!r}; a console paired with this host "
         f"keeps its own name for it until it's renamed there")
    return 0


__all__ = ["cmd_rename"]
