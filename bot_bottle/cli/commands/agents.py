"""agents: the agent definitions in ~/.bot-bottle/agents and
./.bot-bottle/agents, and which one a bare `bot-bottle` starts.

    bot-bottle agents                   list them (the default marked)
    bot-bottle agents default <agent>   start <agent> where none was used last
    bot-bottle agents migrate <agent> [--yes]
                                        rewrite an agent in an old format in
                                        the current one, as a new version
"""

from __future__ import annotations

import os
import sys

from ...manifest import ManifestIndex
from ..constants import PROG


def cmd_agents(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    # pylint: disable-next=import-outside-toplevel
    from .quick import cmd_default, default_agent
    if argv and argv[0] == "default":
        return cmd_default(argv[1:])
    if argv and argv[0] == "migrate":
        # pylint: disable-next=import-outside-toplevel
        from .bottle import migrate_agent
        return migrate_agent(argv[1:])
    if argv:
        sys.stderr.write(f"{PROG} agents: unknown argument {argv[0]!r}\n\n{__doc__}")
        return 2
    default = default_agent()
    tty = sys.stdout.isatty()
    for name in ManifestIndex.resolve(os.getcwd()).all_agent_names:
        print(f"{name}  (default)" if tty and name == default else name)
    return 0


__all__ = ["cmd_agents"]
