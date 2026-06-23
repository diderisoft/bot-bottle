"""Unit: macos-container pty_forward raw-mode wrapper (issue #245).

Tests argument parsing, non-TTY fallback, and the raw-mode
setup/restore sequence without requiring a real terminal.
"""

from __future__ import annotations

import io
import termios
import unittest
from unittest.mock import ANY, MagicMock, patch

from bot_bottle.backend.macos_container import pty_forward


def _fake_stdin(fd: int = 0) -> MagicMock:
    """Return a mock stdin whose fileno() returns *fd*."""
    m = MagicMock()
    m.fileno.return_value = fd
    return m


class TestArgvParsing(unittest.TestCase):
    def test_missing_separator_returns_error_exit_code(self):
        with patch.object(pty_forward.sys, "stderr", new=io.StringIO()) as err:
            rc = pty_forward.main(["container", "exec"])
        self.assertEqual(2, rc)
        self.assertIn("usage:", err.getvalue())

    def test_too_few_args_returns_error_exit_code(self):
        with patch.object(pty_forward.sys, "stderr", new=io.StringIO()):
            self.assertEqual(2, pty_forward.main([]))
            self.assertEqual(2, pty_forward.main(["--"]))

    def test_separator_at_start_with_inner_is_valid(self):
        with (
            patch.object(pty_forward.sys, "stdin", _fake_stdin()),
            patch.object(pty_forward.os, "isatty", return_value=False),
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            rc = pty_forward.main(["--", "container", "exec"])
        self.assertEqual(0, rc)
        run.assert_called_once()
        self.assertEqual(["container", "exec"], run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs["check"])


class TestNonTtyFallback(unittest.TestCase):
    def test_non_tty_stdin_runs_inner_directly(self):
        with (
            patch.object(pty_forward.sys, "stdin", _fake_stdin()),
            patch.object(pty_forward.os, "isatty", return_value=False),
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 42
            rc = pty_forward.main(
                ["--", "container", "exec", "--interactive", "--tty", "c", "claude"]
            )
        self.assertEqual(42, rc)
        run.assert_called_once()
        self.assertEqual(
            ["container", "exec", "--interactive", "--tty", "c", "claude"],
            run.call_args.args[0],
        )
        self.assertFalse(run.call_args.kwargs["check"])

    def test_fileno_error_runs_inner_directly(self):
        bad_stdin = MagicMock()
        bad_stdin.fileno.side_effect = OSError("pseudofile")
        with (
            patch.object(pty_forward.sys, "stdin", bad_stdin),
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            rc = pty_forward.main(["--", "container", "exec"])
        run.assert_called_once()
        self.assertEqual(["container", "exec"], run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertEqual(0, rc)


class TestRawModeSetupAndRestore(unittest.TestCase):
    def test_tty_stdin_sets_raw_mode_and_restores_on_exit(self):
        saved_attrs = object()
        with (
            patch.object(pty_forward.sys, "stdin", _fake_stdin()),
            patch.object(pty_forward.os, "isatty", return_value=True),
            patch.object(pty_forward.termios, "tcgetattr", return_value=saved_attrs),
            patch.object(pty_forward.tty, "setraw") as setraw,
            patch.object(pty_forward.termios, "tcsetattr") as tcsetattr,
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            rc = pty_forward.main(["--", "container", "exec"])

        self.assertEqual(0, rc)
        setraw.assert_called_once()
        tcsetattr.assert_called_once_with(
            ANY, termios.TCSADRAIN, saved_attrs,
        )

    def test_tty_restores_on_subprocess_nonzero_exit(self):
        saved_attrs = object()
        with (
            patch.object(pty_forward.sys, "stdin", _fake_stdin()),
            patch.object(pty_forward.os, "isatty", return_value=True),
            patch.object(pty_forward.termios, "tcgetattr", return_value=saved_attrs),
            patch.object(pty_forward.tty, "setraw"),
            patch.object(pty_forward.termios, "tcsetattr") as tcsetattr,
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 1
            rc = pty_forward.main(["--", "container", "exec"])

        self.assertEqual(1, rc)
        tcsetattr.assert_called_once_with(
            ANY, termios.TCSADRAIN, saved_attrs,
        )

    def test_tcgetattr_error_falls_back_to_bare_run(self):
        with (
            patch.object(pty_forward.sys, "stdin", _fake_stdin()),
            patch.object(pty_forward.os, "isatty", return_value=True),
            patch.object(
                pty_forward.termios, "tcgetattr",
                side_effect=termios.error("not a tty"),
            ),
            patch.object(pty_forward.tty, "setraw") as setraw,
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            rc = pty_forward.main(["--", "container", "exec"])

        setraw.assert_not_called()
        run.assert_called_once()
        self.assertEqual(["container", "exec"], run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertEqual(0, rc)

    def test_inner_run_sets_term_default_without_mutating_process_env(self):
        with (
            patch.dict(pty_forward.os.environ, {}, clear=True),
            patch.object(pty_forward.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            rc = pty_forward._run_inner(["container", "exec"])

            self.assertNotIn("TERM", pty_forward.os.environ)

        self.assertEqual(0, rc)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(["TERM"], sorted(child_env.keys()))
        self.assertEqual("xterm-256color", child_env["TERM"])


if __name__ == "__main__":
    unittest.main()
