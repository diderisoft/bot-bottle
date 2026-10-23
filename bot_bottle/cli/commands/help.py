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
    w("  active    list currently-running bot-bottle bottles\n")
    w("  attach    join a running agent session in this terminal (Ctrl-] detaches)\n")
    w("  backend   set up / check / undo a backend's host prerequisites (setup|status|teardown)\n")
    w("  cleanup   stop and remove all active bot-bottle containers\n")
    w("  commit    snapshot a running bottle's container state to a Docker image\n")
    w("  doctor    check host prerequisites (Python, backend, config dir)\n")
    w("  down      stop the orchestrator, gateway and host controller\n")
    w("  edit      open an agent in vim for editing\n")
    w("  help      show this command list\n")
    w("  init      interactively create a new agent and add it to bot-bottle.json\n")
    w("  install   set up this host: backend prerequisites, tmux check, autostart\n")
    w("  jobs      list the host controller's recent jobs\n")
    w("  list      list available agents from bot-bottle.json\n")
    w("  login     register this host with a bot-bottle console\n")
    w("  resume    re-launch a bottle by its identity (continues its saved state)\n")
    w("  start     boot a container for a named agent and attach an interactive session\n")
    w("  status    show the host controller, orchestrator and running bottles\n")
    w("  supervise view + approve/modify/reject pending supervise proposals\n")
    w("  uninstall stop bot-bottle on this host and remove autostart\n")
    w("  up        start the host controller, orchestrator and gateway (--restart)\n\n")
    w(f"Run '{PROG} <command> --help' for command-specific usage.\n")
    return 0
