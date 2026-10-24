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
    w("  active    list currently-running bot-bottle bottles\n")
    w("  approve   answer an agent's requests (Ctrl-\\ in its session)\n")
    w("  attach    join a running agent session in this terminal (Ctrl-] detaches)\n")
    w("  audit     check, search and configure the audit journal "
      "(tail|verify|query|export|rebuild|import|delivery|forward|firehose)\n")
    w("  backend   set up / check / undo a backend's host prerequisites (setup|status|teardown)\n")
    w("  cleanup   stop and remove all active bot-bottle containers\n")
    w("  commit    snapshot a running bottle's container state to a Docker image\n")
    w("  default   set the agent bare `bot-bottle` starts when none was used here\n")
    w("  doctor    check host prerequisites (Python, backend, config dir)\n")
    w("  down      stop the orchestrator, gateway and host controller\n")
    w("  help      show this command list\n")
    w("  install   set up this host: backend prerequisites, tmux check, autostart\n")
    w("  jobs      list the host controller's recent jobs\n")
    w("  list      list available agents (~/.bot-bottle/agents and ./.bot-bottle/agents)\n")
    w("  login     register this host with a bot-bottle console\n")
    w("  pair      link this host to your phone (QR code + passkey)\n")
    w("  resume    re-launch a bottle by its identity (continues its saved state)\n")
    w("  setup     write starter agents for Claude, Codex and pi (then offers pair)\n")
    w("  start     boot a container for a named agent and attach an interactive session\n")
    w("  status    show the host controller, orchestrator and running bottles\n")
    w("  supervise view + approve/modify/reject pending supervise proposals\n")
    w("  trust     review and trust a project's bottles on this host\n")
    w("  uninstall stop bot-bottle on this host and remove autostart\n")
    w("  up        start the host controller, orchestrator and gateway (--restart)\n\n")
    w(f"Run '{PROG} <command> --help' for command-specific usage.\n")
    return 0
