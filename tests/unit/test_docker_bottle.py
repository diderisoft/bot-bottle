"""Unit: DockerBottle's argv builder (PRD 0021 chunk 1).

`claude_argv` is the pure helper that `exec_claude` and the
PRD-0021 tmux helpers both build on. It encodes two non-trivial
rules — the optional `--append-system-prompt-file` flag and the
optional `-it` for TTY mode — that we lock down here so the tmux
path can rely on identical behavior.
"""

from __future__ import annotations

import unittest

from claude_bottle.backend.docker.bottle import DockerBottle


def _bottle(prompt_path: str | None = None) -> DockerBottle:
    return DockerBottle(
        container="claude-bottle-dev-abc",
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
    )


class TestClaudeArgv(unittest.TestCase):
    def test_minimal_argv_no_prompt(self):
        argv = _bottle().claude_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "claude-bottle-dev-abc", "claude"],
            argv,
        )

    def test_appends_passed_args_after_claude(self):
        argv = _bottle().claude_argv(
            ["--dangerously-skip-permissions", "--continue"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "claude-bottle-dev-abc", "claude",
             "--dangerously-skip-permissions", "--continue"],
            argv,
        )

    def test_appends_prompt_file_flag_when_set(self):
        argv = _bottle("/home/node/.claude-bottle-prompt.txt").claude_argv(
            ["--dangerously-skip-permissions"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "claude-bottle-dev-abc", "claude",
             "--dangerously-skip-permissions",
             "--append-system-prompt-file",
             "/home/node/.claude-bottle-prompt.txt"],
            argv,
        )

    def test_no_prompt_flag_when_none(self):
        argv = _bottle(None).claude_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_empty_prompt_string_is_treated_as_no_prompt(self):
        # Matches the existing exec_claude behavior: falsy
        # prompt_path means "skip the flag." The synth path in
        # dashboard.py relies on this when metadata is missing.
        argv = _bottle("").claude_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_tty_false_drops_it_flag(self):
        argv = _bottle().claude_argv([], tty=False)
        self.assertEqual(
            ["docker", "exec", "claude-bottle-dev-abc", "claude"],
            argv,
        )

    def test_caller_argv_not_mutated(self):
        # `claude_argv` builds `full_argv` from a copy, so a
        # caller passing a long-lived list (e.g., the dashboard's
        # _claude_args fixture) doesn't get extra flags appended to
        # it on subsequent calls.
        original = ["--continue"]
        _bottle("/x").claude_argv(original)
        self.assertEqual(["--continue"], original)


if __name__ == "__main__":
    unittest.main()
