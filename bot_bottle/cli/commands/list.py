"""list: list available agents."""

from __future__ import annotations

import os
import sys

from ...manifest import ManifestIndex
from ..constants import PROG


def cmd_list(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} list\n")
        sys.stderr.write("\nList the agents in ~/.bot-bottle/agents and ./.bot-bottle/agents.\n")
        return 0

    manifest = ManifestIndex.resolve(os.getcwd())
    for name in manifest.all_agent_names:
        print(name)
    return 0
