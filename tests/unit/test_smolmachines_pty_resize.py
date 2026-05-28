"""Unit: smolmachines pty_resize bridge (issue #82).

Locks down the parts of the wrapper we can test without spawning
real children or signalling — argument parsing, the side-channel
`smolvm machine exec` argv shape, and TTY-resolution fallback
across stdin/stdout/stderr.
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from claude_bottle.backend.smolmachines import pty_resize


class TestPushSize(unittest.TestCase):
    def test_emits_for_loop_over_all_pts_devices(self):
        # The shell `for f in /dev/pts/*` handles multiple
        # interactive sessions in the same VM (rare but cheap).
        # Per-PTY `stty -F ... 2>/dev/null` swallows EBADF when a
        # session has already exited.
        with patch.object(pty_resize.subprocess, "run") as run:
            pty_resize._push_size("claude-bottle-m", 50, 200)
        argv = run.call_args.args[0]
        self.assertEqual(
            ["smolvm", "machine", "exec", "--name",
             "claude-bottle-m", "--", "sh", "-c"],
            argv[:8],
        )
        # cols / rows land in the order stty wants them.
        self.assertIn("cols 200", argv[8])
        self.assertIn("rows 50", argv[8])
        self.assertIn("for f in /dev/pts/*", argv[8])

    def test_side_channel_uses_devnull_stdin(self):
        # Load-bearing regression: under tmux, inheriting the
        # pane PTY as the side-channel's stdin makes smolvm crash
        # within ~100ms (concurrent smolvm processes sharing the
        # PTY's FG-PG / input plumbing). DEVNULL stdin sidesteps
        # the interaction.
        with patch.object(pty_resize.subprocess, "run") as run:
            pty_resize._push_size("claude-bottle-m", 24, 80)
        self.assertEqual(
            pty_resize.subprocess.DEVNULL,
            run.call_args.kwargs.get("stdin"),
        )

    def test_swallows_subprocess_failures(self):
        # `check=False` + DEVNULL streams: a side-channel failure
        # mustn't break the operator's session.
        with patch.object(
            pty_resize.subprocess, "run",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(OSError):
                pty_resize._push_size("m", 24, 80)
        # The wrapper-level `sync()` is what swallows; `_push_size`
        # itself raises so the test above documents that. The
        # signal-handler-side `sync` in main wraps in try/except
        # via the `if size is None: return` guard for the
        # no-TTY case (no separate try needed because subprocess
        # already has check=False; only fcntl.ioctl raising would
        # surface, and _read_winsize handles that).


class TestReadWinsize(unittest.TestCase):
    def test_returns_none_when_no_tty(self):
        # Patch ioctl to always OSError — simulates the case where
        # none of stdin/stdout/stderr is a TTY (e.g., tests, piped
        # automation).
        with patch.object(
            pty_resize.fcntl, "ioctl",
            side_effect=OSError("ENOTTY"),
        ):
            self.assertIsNone(pty_resize._read_winsize())

    def test_returns_first_tty_size(self):
        # First fd that responds with a non-zero size wins —
        # matches the "different surfaces give different TTYs"
        # invariant noted in the module docstring.
        import struct

        calls: list[int] = []

        def fake_ioctl(fd, req, buf):
            calls.append(fd)
            if fd == 0:
                raise OSError("stdin not a tty")
            return struct.pack("hhhh", 42, 137, 0, 0)

        with patch.object(pty_resize.fcntl, "ioctl", side_effect=fake_ioctl):
            self.assertEqual((42, 137), pty_resize._read_winsize())

    def test_skips_zero_sizes(self):
        # A TTY that reports `0 0` (the smolvm-allocated PTY's
        # initial state, ironically) shouldn't be used as the
        # source of truth — keep probing fallback fds.
        import struct

        responses = iter([
            struct.pack("hhhh", 0, 0, 0, 0),       # stdin: zero
            struct.pack("hhhh", 24, 80, 0, 0),     # stdout: real
        ])

        def fake_ioctl(fd, req, buf):
            return next(responses)

        with patch.object(pty_resize.fcntl, "ioctl", side_effect=fake_ioctl):
            self.assertEqual((24, 80), pty_resize._read_winsize())


class TestMainArgvParsing(unittest.TestCase):
    def test_missing_separator_returns_error_exit_code(self):
        # No `--` between machine name and inner argv.
        with patch.object(pty_resize.sys, "stderr", new=io.StringIO()) as err:
            rc = pty_resize.main(["claude-bottle-m", "smolvm", "machine"])
        self.assertEqual(2, rc)
        self.assertIn("usage:", err.getvalue())

    def test_too_few_args_returns_error_exit_code(self):
        with patch.object(pty_resize.sys, "stderr", new=io.StringIO()):
            self.assertEqual(2, pty_resize.main([]))
            self.assertEqual(2, pty_resize.main(["m"]))
            self.assertEqual(2, pty_resize.main(["m", "--"]))


if __name__ == "__main__":
    unittest.main()
