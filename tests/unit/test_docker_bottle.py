"""Unit: DockerBottle's argv builder (PRD 0021 chunk 1).

`claude_argv` is the pure helper that `exec_claude` and the
PRD-0021 tmux helpers both build on. It encodes two non-trivial
rules — the optional `--append-system-prompt-file` flag and the
optional `-it` for TTY mode — that we lock down here so the tmux
path can rely on identical behavior.
"""

from __future__ import annotations

import unittest

from bot_bottle.backend.docker.bottle import DockerBottle


def _bottle(prompt_path: str | None = None) -> DockerBottle:
    return DockerBottle(
        container="bot-bottle-dev-abc",
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
    )


def _codex_bottle(prompt_path: str | None = None) -> DockerBottle:
    return DockerBottle(
        container="bot-bottle-dev-abc",
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
        agent_command="codex",
        agent_prompt_mode="codex_read_prompt_file",
    )


class TestClaudeArgv(unittest.TestCase):
    def test_minimal_argv_no_prompt(self):
        argv = _bottle().claude_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "claude"],
            argv,
        )

    def test_appends_passed_args_after_claude(self):
        argv = _bottle().claude_argv(
            ["--dangerously-skip-permissions", "--continue"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "claude",
             "--dangerously-skip-permissions", "--continue"],
            argv,
        )

    def test_appends_prompt_file_flag_when_set(self):
        argv = _bottle("/home/node/.bot-bottle-prompt.txt").claude_argv(
            ["--dangerously-skip-permissions"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "claude",
             "--dangerously-skip-permissions",
             "--append-system-prompt-file",
             "/home/node/.bot-bottle-prompt.txt"],
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
            ["docker", "exec", "bot-bottle-dev-abc", "claude"],
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

    def test_codex_provider_uses_codex_command(self):
        argv = _codex_bottle().claude_argv(
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "--dangerously-bypass-approvals-and-sandbox"],
            argv,
        )

    def test_codex_provider_passes_prompt_reference_as_initial_prompt(self):
        argv = _codex_bottle("/home/node/.bot-bottle-prompt.txt").claude_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "Read and follow the instructions in "
             "/home/node/.bot-bottle-prompt.txt."],
            argv,
        )

    def test_codex_resume_does_not_append_initial_prompt(self):
        argv = _codex_bottle("/home/node/.bot-bottle-prompt.txt").claude_argv(
            ["--dangerously-bypass-approvals-and-sandbox", "resume", "--last"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "--dangerously-bypass-approvals-and-sandbox", "resume", "--last"],
            argv,
        )


if __name__ == "__main__":
    unittest.main()
