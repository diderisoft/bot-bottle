"""Host-side SIGWINCH → in-VM PTY resize bridge (issue #82).

smolvm 0.8.0 `machine exec -t` allocates an in-VM PTY but never
forwards the host terminal's window size (TIOCSWINSZ) to it. The
PTY's initial size is `0 0`, and any host-side resize during the
session goes unnoticed — the in-VM claude TUI keeps rendering for
whatever (typically tiny) box it last saw, ignoring the operator's
tmux pane resize. `docker exec -it` does this forwarding
automatically; smolvm doesn't.

This module wraps `smolvm machine exec` with a thin parent
process that:

  1. Spawns the original argv as a child (it gets the inherited
     TTY, so claude's stdin/stdout/stderr work unchanged).
  2. On startup + every host SIGWINCH, reads the host terminal
     size via TIOCGWINSZ on stdin (or stderr if stdin isn't a
     TTY — tmux respawn-pane gives us a TTY on stdout/stderr)
     and pushes it into the VM with a side-channel
     `smolvm machine exec -- sh -c 'for f in /dev/pts/*; do
     stty -F $f cols X rows Y; done'`. The kernel delivers
     SIGWINCH to the foreground process group on the slave end
     automatically, so claude picks up the new size without
     extra signalling.
  3. Waits on the child and exits with its returncode.

The dashboard's tmux pane respawn calls `bottle.agent_argv`
which now prepends `[sys.executable, -m, ..., <machine>, --, ...]`
to the smolvm argv. Foreground handoff (curses endwin →
subprocess.run) goes through the same path so behavior is
identical.

Removable once smolvm grows native SIGWINCH forwarding (upstream
follow-up tracked separately)."""

from __future__ import annotations

import fcntl
import signal
import struct
import subprocess
import sys
import termios
import threading
from types import FrameType


# How long to wait after the main exec starts before pushing the
# initial size. Concurrent `smolvm machine exec` invocations race
# libkrun's per-exec OCI config write during the main exec's
# bringup window; the side-channel firing immediately corrupts
# `config.json` and the main exec dies with SIGKILL (rc=137) or
# libkrun's "parse error: trailing garbage" depending on
# scheduling. Two seconds is well past the bringup window on a
# warm VM, well under the operator's "this is unresponsive"
# threshold, and short enough that claude's initial render
# almost always fires after the size has been set.
_STARTUP_SYNC_DELAY_SEC = 2.0


def _read_winsize() -> tuple[int, int] | None:
    """Return `(rows, cols)` from whichever of stdin / stdout /
    stderr is a TTY, or None if none are. Different invocation
    surfaces give us different TTYs:

      - foreground handoff (curses endwin → subprocess.run): all
        three are the operator's terminal.
      - tmux respawn-pane: tmux sets all three to the pane's PTY.
      - non-TTY (someone piped stdin in tests): none are; the
        sync just no-ops, which is the right behavior."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
            data = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        except OSError:
            continue
        rows, cols, _, _ = struct.unpack("hhhh", data)
        if rows > 0 and cols > 0:
            return rows, cols
    return None


def _push_size(machine: str, rows: int, cols: int) -> None:
    """Side-channel `smolvm machine exec` that sets the size of
    every PTY in the VM. The shell `for` loop covers the case of
    multiple concurrent interactive sessions (rare but cheap to
    handle); `stty -F` returns silently on PTYs that don't apply.

    Best-effort: swallow failures. A failed resize doesn't break
    the session — it just leaves the in-VM PTY at its old size.

    `stdin=DEVNULL` is load-bearing: under tmux, inheriting the
    pane PTY here means two concurrent smolvm processes (this one
    and the agent session the wrapper is shepherding) share the
    PTY's foreground-process-group / input plumbing, and smolvm
    bails with an internal config-parse error or SIGKILL within
    ~100ms of the side-channel firing. Outside tmux the same
    pattern survived, presumably because iTerm's PTY plumbing is
    more forgiving than tmux's, but the DEVNULL is the right
    default either way — the side-channel never needs stdin."""
    subprocess.run(
        ["smolvm", "machine", "exec", "--name", machine, "--",
         "sh", "-c",
         f"for f in /dev/pts/*; do "
         f"stty -F \"$f\" cols {cols} rows {rows} 2>/dev/null; "
         f"done"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )


def main(argv: list[str]) -> int:
    """Entry point. `argv` shape: `<machine> -- <smolvm-argv...>`.

    We don't use argparse — the `--` separator is the contract and
    everything past it is forwarded verbatim. Keeps the wrapper
    transparent for callers building argv programmatically."""
    if len(argv) < 3 or argv[1] != "--":
        sys.stderr.write(
            "usage: python -m bot_bottle.backend.smolmachines.pty_resize "
            "<machine> -- <smolvm-argv...>\n"
        )
        return 2
    machine = argv[0]
    inner = argv[2:]

    def sync(_signum: int | None = None, _frame: FrameType | None = None) -> None:
        size = _read_winsize()
        if size is None:
            return
        _push_size(machine, *size)

    signal.signal(signal.SIGWINCH, sync)  # type: ignore[arg-type]

    proc = subprocess.Popen(inner)
    # Initial sync is deferred — see _STARTUP_SYNC_DELAY_SEC.
    # daemon=True so the timer doesn't block exit when the child
    # finishes before the delay elapses.
    timer = threading.Timer(_STARTUP_SYNC_DELAY_SEC, sync)
    timer.daemon = True
    timer.start()
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
