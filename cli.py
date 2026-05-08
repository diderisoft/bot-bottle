#!/usr/bin/env python3
"""cli.py — manage claude-bottle containers.

usage: cli.py <command> [args...]

Commands:
  build     build (or rebuild) the claude-bottle Docker image.
  cleanup   stop and remove all active claude-bottle containers.
  edit      open an agent in vim for editing.
  info      print env, skills, and prompt details for a named agent.
  init      interactively create a new agent and add it to claude-bottle.json.
  list      list available agents or active containers.
  start     boot a sandboxed container for a named agent and attach an
            interactive claude-code session. The container is torn down
            when the session ends.
"""

from __future__ import annotations

import sys

from claude_bottle.cli import main

if __name__ == "__main__":
    sys.exit(main())
