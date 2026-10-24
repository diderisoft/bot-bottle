"""A session's pane command: fetch the real command and env from the host
controller, run it, and exit as it did. usage: launch <socket> <session-id>

It waits rather than execs and reports the exit itself, since tmux 3.4 doesn't
always fire `pane-died` for a pane that exits right after EOF.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from .localsock import request


def main(argv: list[str]) -> int:
    sock, session_id = argv
    try:
        reply = request(Path(sock), {"op": "spec", "session": session_id})
    except OSError as e:
        sys.stderr.write(f"bot-bottle: can't reach the host controller: {e}\n")
        return 127
    if "error" in reply:
        sys.stderr.write(f"bot-bottle: {reply['error']}\n")
        return 127
    command = [str(a) for a in reply["argv"]]  # type: ignore[union-attr]
    env = {**os.environ, **{str(k): str(v) for k, v in reply["env"].items()}}  # type: ignore[union-attr]
    try:
        proc = subprocess.Popen(command, env=env)  # pylint: disable=consider-using-with
    except OSError as e:
        sys.stderr.write(f"bot-bottle: can't start {command[0]}: {e}\n")
        return 127
    # The agent gets Ctrl-C from the terminal; pass on what's sent to us.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGQUIT, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, _f: proc.send_signal(s))
    code = proc.wait()
    # The pane-died hook is the backup if this launcher is killed.
    status, sig = (0, signal.Signals(-code).name) if code < 0 else (code, "")
    try:
        request(Path(sock), {"op": "died", "session": session_id,
                             "status": str(status), "signal": sig})
    except OSError:
        pass  # daemon down; it finishes the session on re-adopt
    if code < 0:  # killed by a signal: die the same way
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
