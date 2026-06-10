"""Unit: DockerBottle's argv builder.

`agent_argv` is the pure helper for constructing docker exec command
arguments. It encodes two non-trivial rules — the optional
`--append-system-prompt-file` flag and the optional `-it` for TTY mode
— that we lock down here so callers can rely on consistent behavior.
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
        agent_prompt_mode="read_prompt_file",
    )


def _pi_bottle(prompt_path: str | None = None) -> DockerBottle:
    return DockerBottle(
        container="bot-bottle-dev-abc",
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
        agent_command="pi",
        agent_prompt_mode="append_system_prompt",
    )


def _workspace_bottle() -> DockerBottle:
    return DockerBottle(
        container="bot-bottle-dev-abc",
        teardown=lambda: None,
        prompt_path_in_container=None,
        agent_workdir="/home/node/workspace",
    )


class TestClaudeArgv(unittest.TestCase):
    def test_minimal_argv_no_prompt(self):
        argv = _bottle().agent_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "claude"],
            argv,
        )

    def test_appends_passed_args_after_claude(self):
        argv = _bottle().agent_argv(
            ["--dangerously-skip-permissions", "--continue"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "claude",
             "--dangerously-skip-permissions", "--continue"],
            argv,
        )

    def test_appends_prompt_file_flag_when_set(self):
        argv = _bottle("/home/node/.bot-bottle-prompt.txt").agent_argv(
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
        argv = _bottle(None).agent_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_empty_prompt_string_is_treated_as_no_prompt(self):
        # Matches the existing exec_agent behavior: falsy
        # prompt_path means "skip the flag." The synth path in
        # dashboard.py relies on this when metadata is missing.
        argv = _bottle("").agent_argv(["--continue"])
        self.assertNotIn("--append-system-prompt-file", argv)

    def test_tty_false_drops_it_flag(self):
        argv = _bottle().agent_argv([], tty=False)
        self.assertEqual(
            ["docker", "exec", "bot-bottle-dev-abc", "claude"],
            argv,
        )

    def test_workspace_workdir_is_used_when_set(self):
        argv = _workspace_bottle().agent_argv([])
        self.assertEqual(
            [
                "docker", "exec", "-it", "-w", "/home/node/workspace",
                "bot-bottle-dev-abc", "claude",
            ],
            argv,
        )

    def test_caller_argv_not_mutated(self):
        # `agent_argv` builds `full_argv` from a copy, so a
        # caller passing a long-lived list (e.g., the dashboard's
        # _agent_args fixture) doesn't get extra flags appended to
        # it on subsequent calls.
        original = ["--continue"]
        _bottle("/x").agent_argv(original)
        self.assertEqual(["--continue"], original)

    def test_codex_provider_uses_codex_command(self):
        argv = _codex_bottle().agent_argv(
            ["--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "--dangerously-bypass-approvals-and-sandbox"],
            argv,
        )

    def test_codex_provider_passes_prompt_reference_as_initial_prompt(self):
        argv = _codex_bottle("/home/node/.bot-bottle-prompt.txt").agent_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "Read and follow the instructions in "
             "/home/node/.bot-bottle-prompt.txt."],
            argv,
        )

    def test_codex_resume_does_not_append_initial_prompt(self):
        argv = _codex_bottle("/home/node/.bot-bottle-prompt.txt").agent_argv(
            ["--dangerously-bypass-approvals-and-sandbox", "resume", "--last"],
        )
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "codex",
             "--dangerously-bypass-approvals-and-sandbox", "resume", "--last"],
            argv,
        )

    def test_pi_provider_appends_system_prompt_without_print_mode(self):
        argv = _pi_bottle("/home/node/.bot-bottle-prompt.txt").agent_argv([])
        self.assertEqual(
            ["docker", "exec", "-it", "bot-bottle-dev-abc", "pi",
             "--append-system-prompt", "/home/node/.bot-bottle-prompt.txt"],
            argv,
        )
        self.assertNotIn("-p", argv)


if __name__ == "__main__":
    unittest.main()
