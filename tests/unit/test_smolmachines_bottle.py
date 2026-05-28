"""Unit: SmolmachinesBottle's `claude_argv` builder.

The dashboard's tmux pane-respawn path calls `bottle.claude_argv`
directly (it spawns claude inside a tmux pane rather than as a
child of the current process), so the argv shape is the
non-trivial part. `exec_claude` is a thin wrapper around the same
builder + `subprocess.run`; we lock the shape here.

The TTY-mode argv is wrapped in the pty_resize helper (issue #82
workaround); we assert both the wrapper presence and the wrapped
smolvm argv shape. Non-TTY mode skips the wrapper.
"""

from __future__ import annotations

import sys
import unittest

from claude_bottle.backend.smolmachines import pty_resize as _pty_resize
from claude_bottle.backend.smolmachines.bottle import SmolmachinesBottle


def _bottle(prompt_path: str | None = None, **env: str) -> SmolmachinesBottle:
    return SmolmachinesBottle(
        "claude-bottle-dev-abc",
        prompt_path=prompt_path,
        guest_env=env,
    )


def _unwrap(argv: list[str]) -> list[str]:
    """Strip the pty_resize wrapper from the front of a TTY-mode
    argv, return the inner smolvm argv. Mirrors what the kernel
    sees inside the wrapper's `subprocess.Popen`."""
    idx = argv.index("--")
    return argv[idx + 1:]


class TestClaudeArgvWrapped(unittest.TestCase):
    """TTY-mode argv: pty_resize wrapper + inner smolvm exec."""

    def test_pty_resize_wrapper_prefix(self):
        argv = _bottle().claude_argv([])
        # Absolute script path (not `-m <dotted>`) so the tmux
        # pane's cwd doesn't matter — see the `_PTY_RESIZE_SCRIPT`
        # docstring in bottle.py.
        self.assertEqual(
            [
                sys.executable, _pty_resize.__file__,
                "claude-bottle-dev-abc", "--",
            ],
            argv[:4],
        )

    def test_minimal_inner_argv_no_prompt(self):
        argv = _unwrap(_bottle().claude_argv([]))
        self.assertEqual(
            [
                "smolvm", "machine", "exec", "--name",
                "claude-bottle-dev-abc",
                "-i", "-t",
                "-e", "HOME=/home/node",
                "-e", "USER=node",
                "--",
                "runuser", "-u", "node", "--",
                "claude",
            ],
            argv,
        )

    def test_appends_passed_args_after_claude(self):
        argv = _unwrap(_bottle().claude_argv(
            ["--dangerously-skip-permissions", "--continue"],
        ))
        self.assertEqual(
            ["claude", "--dangerously-skip-permissions", "--continue"],
            argv[argv.index("claude"):],
        )

    def test_appends_prompt_file_flag_when_set(self):
        argv = _unwrap(
            _bottle("/home/node/.claude-bottle-prompt.txt").claude_argv(
                ["--dangerously-skip-permissions"],
            )
        )
        self.assertEqual(
            [
                "claude",
                "--append-system-prompt-file",
                "/home/node/.claude-bottle-prompt.txt",
                "--dangerously-skip-permissions",
            ],
            argv[argv.index("claude"):],
        )

    def test_no_prompt_flag_when_none(self):
        argv = _bottle(None).claude_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_empty_prompt_string_is_treated_as_no_prompt(self):
        argv = _bottle("").claude_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_guest_env_forwarded_as_e_flags(self):
        argv = _unwrap(_bottle(
            None,
            HTTPS_PROXY="http://127.0.0.1:1234",
            NO_PROXY="localhost",
        ).claude_argv([]))
        self.assertIn("-e", argv)
        self.assertIn("HTTPS_PROXY=http://127.0.0.1:1234", argv)
        self.assertIn("NO_PROXY=localhost", argv)

    def test_runuser_switch_precedes_claude(self):
        # The dashboard's `_build_resume_argv_with_fallback` finds
        # the `claude` token to split exec-framing from the claude
        # tail. `runuser -u node --` must sit on the prefix side so
        # the shell wrap inherits the UID switch.
        argv = _bottle().claude_argv([])
        claude_idx = argv.index("claude")
        self.assertEqual(
            ["runuser", "-u", "node", "--"],
            argv[claude_idx - 4:claude_idx],
        )


class TestClaudeArgvNoTTY(unittest.TestCase):
    """`tty=False` paths skip the pty_resize wrapper — there's no
    PTY whose SIGWINCH we'd need to bridge."""

    def test_no_wrapper_when_tty_false(self):
        argv = _bottle().claude_argv([], tty=False)
        self.assertEqual("smolvm", argv[0])
        self.assertFalse(any("pty_resize" in a for a in argv))

    def test_tty_false_drops_it_flags(self):
        argv = _bottle().claude_argv([], tty=False)
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)


if __name__ == "__main__":
    unittest.main()
