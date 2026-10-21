"""attach: join a running agent session in this terminal (PRD 0092).

Runs `tmux attach` against bot-bottle's own tmux server. Ctrl-] detaches;
the agent keeps running.
"""

from __future__ import annotations

import os
import shutil
import sys

from ...control.hostctl.sessions import SessionManager
from ..constants import PROG


def cmd_attach(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} attach <session>\n")
        sys.stderr.write("\nJoin a running agent session; Ctrl-] detaches.\n")
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    session = argv[0]
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        sys.stderr.write(f"{PROG}: tmux is not installed\n")
        return 1
    manager = SessionManager({})
    if not manager.exists(session):
        sys.stderr.write(f"{PROG}: no running session {session!r}\n")
        return 1
    argv_attach = manager.attach_argv(session, read_only=False)
    argv_attach[0] = tmux_bin
    os.execv(tmux_bin, argv_attach)
    return 1  # not reached
