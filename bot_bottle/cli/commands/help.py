"""help: print the command list and usage, for `help`, `-h`, no args and
unknown commands. Keep the summaries in sync with `COMMANDS`.
"""

from __future__ import annotations

import sys

from ..constants import PROG


def cmd_help(argv: list[str] | None = None) -> int:
    """Write usage and the command list to stderr. Returns 0; the dispatcher
    picks the exit code."""
    del argv  # no arguments
    w = sys.stderr.write
    w(f"usage: {PROG} [agent]           start an agent on this directory\n")
    w(f"       {PROG} <command> [args...]\n\n")
    w("Commands:\n")
    w("  agents    list agent definitions; `agents default <agent>` sets the default\n")
    w("  attach    join a running bottled agent's terminal (Ctrl-] detaches)\n")
    w("  audit     check, search and configure the audit journal "
      "(tail|verify|query|export|rebuild|import|delivery|forward|firehose)\n")
    w("  bootstrap first-time setup: install, setup, then doctor\n")
    w("  bots      list bottled agents, running and stopped (also: bottled-agents)\n")
    w("  bottles   list bottle definitions; their versions (history|revert)\n")
    w("  cleanup   remove leftover bottled agents (bots) or unneeded cached images (images)\n")
    w("  commit    snapshot a running bottled agent's container state to an image\n")
    w("  doctor    check host prerequisites (Python, backends, tmux, config dir)\n")
    w("  down      stop the orchestrator, gateway and host controller\n")
    w("  forge     connect a forge (Gitea) so bottles can grant its repos in a line\n")
    w("  help      show this command list\n")
    w("  install   set up this host: backend prerequisites, tmux, autostart (--undo)\n")
    w("  pair      link this host to a console and your phone (on|off, --password)\n")
    w("  resume    start a stopped bottled agent again (continues its saved state)\n")
    w("  setup     write starter agents for Claude, Codex and pi (then offers pair)\n")
    w("  start     bottle an agent: start it in its bottles and attach its terminal\n")
    w("  status    show the host controller, orchestrator, console link, bottled agents\n")
    w("  supervise answer bottled agents' pending requests (Ctrl-\\ in a terminal)\n")
    w("  trust     review and trust a project's bottles on this host\n")
    w("  uninstall remove bot-bottle from this host (keeps ~/.bot-bottle unless --purge)\n")
    w("  up        start the host controller, orchestrator and gateway (--restart, --build)\n\n")
    w(f"Run '{PROG} <command> --help' for command-specific usage.\n")
    return 0
