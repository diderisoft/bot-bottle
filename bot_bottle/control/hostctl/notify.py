"""Run by tmux's `pane-died` hook: report a session's exit to the host
controller. usage: notify <socket> <session> <status> [signal]"""

from __future__ import annotations

import sys
from pathlib import Path

from .localsock import request


def main(argv: list[str]) -> int:
    sock, session, status = argv[:3]
    signal = argv[3] if len(argv) > 3 else ""
    try:
        request(Path(sock), {"op": "died", "session": session, "status": status, "signal": signal})
    except OSError:
        return 1  # the daemon is down; it finishes the session when it re-adopts
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
