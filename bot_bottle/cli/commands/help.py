"""help: print the top-level command list and usage.

Rendered by the dispatcher for the `help` command and for its
`-h`/`--help`, no-args, and unknown-command fallbacks. The per-command
summaries live here; keep them in sync with the COMMANDS table in
`bot_bottle.cli`.
"""

from __future__ import annotations

import sys

from ..constants import PROG


def cmd_help(argv: list[str] | None = None) -> int:
    """Write the top-level usage + command list to stderr. Returns 0;
    the dispatcher chooses the process exit code per entry path (0 for an
    explicit `help`/`-h`, 2 for the bare no-args usage error)."""
    del argv  # help takes no arguments
    w = sys.stderr.write
    w(f"usage: {PROG} <command> [args...]\n\n")
    w("Commands:\n")
    w("  backend   set up / check / undo a backend's host prerequisites (setup|status|teardown)\n")
    w("  cleanup   stop and remove all active bot-bottle containers\n")
    w("  commit    snapshot a running bottle's container state to a Docker image\n")
    w("  edit      open an agent in vim for editing\n")
    w("  help      show this command list\n")
    w("  info      print env, skills, and prompt details for a named agent\n")
    w("  init      interactively create a new agent and add it to bot-bottle.json\n")
    w("  list      list available agents or active containers\n")
    w("  login     register this host with a bot-bottle console\n")
    w("  resume    re-launch a bottle by its identity (continues state from PRD 0016)\n")
    w("  start     boot a container for a named agent and attach an interactive session\n")
    w("  supervise view + approve/modify/reject pending supervise proposals (PRD 0013)\n\n")
    w(f"Run '{PROG} <command> --help' for command-specific usage.\n")
    return 0
