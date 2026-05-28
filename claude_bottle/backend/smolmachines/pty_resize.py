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

The dashboard's tmux pane respawn calls `bottle.claude_argv`
which now prepends `[sys.executable, -m, ..., <machine>, --, ...]`
to the smolvm argv. Foreground handoff (curses endwin →
subprocess.run) goes through the same path so behavior is
identical.

Removable once smolvm grows native SIGWINCH forwarding (upstream
follow-up tracked separately)."""

from __future__ import annotations

import datetime
import fcntl
import os
import signal
import struct
import subprocess
import sys
import termios
import traceback


# Debug log so we can diagnose tmux-pane crashes that happen in
# pane respawn — the dashboard's curses surface eats stderr, and
# `tmux respawn-pane`'s default remain-on-exit is off. Always-on
# (small overhead) so a user reporting a crash can just share the
# file. Append-mode, per-pid line prefix.
_DEBUG_LOG_PATH = os.path.expanduser("~/.claude-bottle/pty_resize.log")


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a") as f:
            ts = datetime.datetime.now().isoformat(timespec="milliseconds")
            f.write(f"[{ts} pid={os.getpid()}] {msg}\n")
    except OSError:
        pass


def _read_winsize() -> tuple[int, int] | None:
    """Return `(rows, cols)` from whichever of stdin / stdout /
    stderr is a TTY, or None if none are. Different invocation
    surfaces give us different TTYs:

      - foreground handoff (curses endwin → subprocess.run): all
        three are the operator's terminal.
      - tmux respawn-pane: tmux sets all three to the pane's PTY.
      - non-TTY (someone piped stdin in tests): none are; the
        sync just no-ops, which is the right behavior."""
    for fd in (sys.stdin.fileno(), sys.stdout.fileno(), sys.stderr.fileno()):
        try:
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
    the session — it just leaves the in-VM PTY at its old size."""
    subprocess.run(
        ["smolvm", "machine", "exec", "--name", machine, "--",
         "sh", "-c",
         f"for f in /dev/pts/*; do "
         f"stty -F \"$f\" cols {cols} rows {rows} 2>/dev/null; "
         f"done"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )


def main(argv: list[str]) -> int:
    """Entry point. `argv` shape: `<machine> -- <smolvm-argv...>`.

    We don't use argparse — the `--` separator is the contract and
    everything past it is forwarded verbatim. Keeps the wrapper
    transparent for callers building argv programmatically."""
    _log(f"start argv={argv!r} cwd={os.getcwd()!r} "
         f"PATH={os.environ.get('PATH','')!r} "
         f"TMUX={os.environ.get('TMUX','<unset>')!r}")

    if len(argv) < 3 or argv[1] != "--":
        sys.stderr.write(
            "usage: python -m claude_bottle.backend.smolmachines.pty_resize "
            "<machine> -- <smolvm-argv...>\n"
        )
        _log("exit=2 (bad argv)")
        return 2
    machine = argv[0]
    inner = argv[2:]

    def sync(*_args) -> None:
        size = _read_winsize()
        _log(f"sync size={size!r}")
        if size is None:
            return
        _push_size(machine, *size)

    # Install BEFORE spawning the child so the first SIGWINCH
    # (e.g., from tmux refreshing the pane right after respawn)
    # is caught even if it races the initial sync.
    signal.signal(signal.SIGWINCH, sync)

    try:
        proc = subprocess.Popen(inner)
    except BaseException:
        _log("Popen failed:\n" + traceback.format_exc())
        raise
    _log(f"child pid={proc.pid}")
    sync()  # push initial size — VM PTY starts at 0 0.
    while True:
        try:
            rc = proc.wait()
            _log(f"child exit rc={rc}")
            return rc
        except KeyboardInterrupt:
            # Ctrl-C in the operator's terminal → forward to the
            # child once, then keep waiting. claude handles its
            # own interrupt cleanup.
            _log("KeyboardInterrupt → forward SIGINT to child")
            proc.send_signal(signal.SIGINT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
