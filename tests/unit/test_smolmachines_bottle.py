"""Unit: SmolmachinesBottle's `claude_argv` builder.

The dashboard's tmux pane-respawn path calls `bottle.claude_argv`
directly (it spawns claude inside a tmux pane rather than as a
child of the current process), so the argv shape is the
non-trivial part. `exec_claude` is a thin wrapper around the same
builder + `subprocess.run`; we lock the shape here.
"""

from __future__ import annotations

import unittest

from claude_bottle.backend.smolmachines.bottle import SmolmachinesBottle


def _bottle(prompt_path: str | None = None, **env: str) -> SmolmachinesBottle:
    return SmolmachinesBottle(
        "claude-bottle-dev-abc",
        prompt_path=prompt_path,
        guest_env=env,
    )


class TestClaudeArgv(unittest.TestCase):
    def test_minimal_argv_no_prompt(self):
        argv = _bottle().claude_argv([])
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
        argv = _bottle().claude_argv(
            ["--dangerously-skip-permissions", "--continue"],
        )
        # The claude tail is at the end of the argv, after the
        # `runuser -u node --` switch.
        self.assertEqual(
            ["claude", "--dangerously-skip-permissions", "--continue"],
            argv[argv.index("claude"):],
        )

    def test_appends_prompt_file_flag_when_set(self):
        argv = _bottle("/home/node/.claude-bottle-prompt.txt").claude_argv(
            ["--dangerously-skip-permissions"],
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

    def test_tty_false_drops_it_flags(self):
        argv = _bottle().claude_argv([], tty=False)
        self.assertNotIn("-i", argv)
        self.assertNotIn("-t", argv)

    def test_guest_env_forwarded_as_e_flags(self):
        argv = _bottle(
            None,
            HTTPS_PROXY="http://127.0.0.1:1234",
            NO_PROXY="localhost",
        ).claude_argv([])
        # `-e K=V` pairs land before the `--`. Order isn't
        # guaranteed across dict iterations on older Pythons, but
        # both must appear.
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


if __name__ == "__main__":
    unittest.main()
