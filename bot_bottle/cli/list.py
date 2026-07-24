"""list: list available agents."""

from __future__ import annotations

import sys

from ..manifest import ManifestIndex
from ._common import PROG, USER_CWD


def cmd_list(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} list\n")
        sys.stderr.write("\nList all available agents from bot-bottle.json.\n")
        return 0

    manifest = ManifestIndex.resolve(USER_CWD)
    for name in manifest.all_agent_names:
        print(name)
    return 0
