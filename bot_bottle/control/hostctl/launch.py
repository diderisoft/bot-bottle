"""A session's pane command: fetch the real command and environment from the
host controller, then exec it. usage: launch <socket> <session-id>"""

from __future__ import annotations

import os
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
    os.execvpe(command[0], command, env)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
