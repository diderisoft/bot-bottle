"""attach: join a running agent session in this terminal.

Runs `tmux attach` against bot-bottle's own tmux server. Ctrl-] detaches;
the agent keeps running.
"""

from __future__ import annotations

import os
import shutil
import sys

from ...control.hostctl.sessions import SessionManager
from ..constants import PROG
from ..launch_client import tmux_attach


def cmd_attach(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} attach <session>\n")
        sys.stderr.write("\nJoin a running agent session; Ctrl-] detaches.\n")
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    session = argv[0]
    if shutil.which("tmux") is None:
        sys.stderr.write(f"{PROG}: tmux is not installed\n")
        return 1
    if not SessionManager({}).exists(session):
        sys.stderr.write(f"{PROG}: no running session {session!r}\n")
        return 1
    argv_attach, env = tmux_attach(session)
    os.execve(argv_attach[0], argv_attach, env)
    return 1  # not reached
