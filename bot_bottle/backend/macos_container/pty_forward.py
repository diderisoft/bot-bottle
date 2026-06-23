"""Host-side raw-mode wrapper for `container exec --interactive --tty`.

Apple's `container exec --interactive --tty` does not set the host terminal to
raw mode before starting its I/O relay.  Without raw mode the kernel line
discipline buffers modifier-key escape sequences (e.g. Shift+Enter in
modifyOtherKeys mode produces \\x1b[13;2~) until a carriage-return arrives, so
they never reach Claude Code inside the container.

This module sets the host terminal to raw mode, spawns the inner argv (the
container exec command), and restores the original terminal attributes on
exit.  When stdin is not a TTY (piped invocations, CI) it falls through to a
bare subprocess.run so callers do not need to special-case non-interactive
contexts.

Usage (the `--` separator is the API contract — everything after it is the
inner command):

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
    """Entry point.  ``argv`` shape: ``-- <inner-argv...>``."""
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
