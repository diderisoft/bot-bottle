"""Raw-mode wrapper for `container exec --interactive --tty`, which doesn't set
the host terminal raw, so modifier-key sequences (e.g. Shift+Enter) would sit
in the line discipline until CR. Restores the terminal on exit; without a TTY
it just runs the command.

    python pty_forward.py -- container exec --interactive --tty <name> <cmd>
"""

from __future__ import annotations

import os
import subprocess
import sys
import termios
import tty


def _inner_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    return env


def _run_inner(inner: list[str]) -> int:
    return subprocess.run(inner, check=False, env=_inner_env()).returncode


def main(argv: list[str]) -> int:
    """Entry point. `argv`: `-- <inner-argv...>`."""
    if len(argv) < 2 or argv[0] != "--":
        sys.stderr.write(
            "usage: python pty_forward.py -- <container-exec-argv...>\n"
        )
        return 2
    inner = argv[1:]

    try:
        fd = sys.stdin.fileno()
    except OSError:
        return _run_inner(inner)

    if not os.isatty(fd):
        return _run_inner(inner)

    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return _run_inner(inner)

    try:
        tty.setraw(fd)
        return _run_inner(inner)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
