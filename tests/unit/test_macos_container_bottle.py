"""Unit: Apple Container bottle command construction."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from bot_bottle.backend.macos_container import bottle as bottle_mod
from bot_bottle.backend.macos_container.bottle import MacosContainerBottle, _PTY_FORWARD_SCRIPT


class TestMacosContainerBottle(unittest.TestCase):
    def test_agent_argv_uses_pty_forward_and_container_exec(self):
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_command="codex",
        )
        with patch.dict(bottle_mod.os.environ, {}, clear=True):
            argv = bottle.agent_argv(["run"])
        self.assertEqual(
            [
                sys.executable, _PTY_FORWARD_SCRIPT, "--",
                "container", "exec", "--interactive", "--tty",
                "--env", "TERM",
                "bot-bottle-dev-abc", "codex", "run",
            ],
            argv,
        )

    def test_agent_argv_accepts_absolute_provider_command(self):
        command = "/home/node/.codex/packages/standalone/current/bin/codex"
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_command=command,
        )
        with patch.dict(bottle_mod.os.environ, {}, clear=True):
            argv = bottle.agent_argv(["run"])
        self.assertIn(command, argv)
        self.assertNotIn("codex", argv)

    def test_agent_argv_includes_workdir(self):
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_workdir="/home/node/workspace",
        )
        with patch.dict(bottle_mod.os.environ, {}, clear=True):
            argv = bottle.agent_argv([])
        self.assertEqual(
            [
                sys.executable, _PTY_FORWARD_SCRIPT, "--",
                "container", "exec", "--interactive", "--tty",
                "--env", "TERM",
                "--workdir", "/home/node/workspace",
                "bot-bottle-dev-abc", "claude",
            ],
            argv,
        )

    def test_agent_argv_forwards_terminal_env_names_without_values(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch.dict(
            bottle_mod.os.environ,
            {
                "TERM": "screen-256color",
                "TERM_PROGRAM": "WezTerm",
                "WEZTERM_PANE": "pane-id",
                "SHELL": "/bin/zsh",
            },
            clear=True,
        ):
            argv = bottle.agent_argv([])
        self.assertIn("TERM", argv)
        self.assertIn("TERM_PROGRAM", argv)
        self.assertIn("WEZTERM_PANE", argv)
        self.assertNotIn("SHELL", argv)
        self.assertNotIn("TERM=screen-256color", argv)
        self.assertNotIn("TERM_PROGRAM=WezTerm", argv)
        self.assertNotIn("WEZTERM_PANE=pane-id", argv)

    def test_agent_argv_always_forwards_term_name(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch.dict(bottle_mod.os.environ, {}, clear=True):
            argv = bottle.agent_argv([])
        self.assertIn("TERM", argv)

    def test_agent_argv_no_tty_omits_wrapper_and_tty_flags(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        argv = bottle.agent_argv([], tty=False)
        self.assertNotIn("--tty", argv)
        self.assertNotIn("--env", argv)
        self.assertNotIn(_PTY_FORWARD_SCRIPT, argv)
        self.assertEqual(["container", "exec", "bot-bottle-dev-abc", "claude"], argv)

    def test_exec_pipes_script_to_shell(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch("bot_bottle.backend.macos_container.bottle.subprocess.run") as run:
            run.return_value.returncode = 7
            run.return_value.stdout = "out"
            run.return_value.stderr = "err"
            result = bottle.exec("echo hi", user="root")
        self.assertEqual(7, result.returncode)
        self.assertEqual(
            [
                "container", "exec", "--user", "root", "--interactive",
                "bot-bottle-dev-abc", "sh", "-s",
            ],
            run.call_args.args[0],
        )
        self.assertEqual("echo hi", run.call_args.kwargs["input"])

    def test_a_script_with_nothing_secret_skips_interactive(self):
        # Every `exec --interactive` leaks a pipe in the bottle's runtime
        # (apple/container#2439): a non-private script goes as an argument.
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch("bot_bottle.backend.macos_container.bottle.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = run.return_value.stderr = ""
            bottle.exec("ls /tmp", private=False)
        self.assertEqual(
            ["container", "exec", "--user", "node", "bot-bottle-dev-abc", "sh", "-c", "ls /tmp"],
            run.call_args.args[0],
        )
        self.assertEqual("", run.call_args.kwargs["input"])

    def test_cp_in_stages_through_tmp(self):
        # `container cp` writes under a volume's mount point, hidden by the
        # volume (the home is one), so it lands in /tmp and moves into place.
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch("bot_bottle.backend.macos_container.bottle.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = run.return_value.stderr = ""
            bottle.cp_in("/tmp/src", "/home/node/src")
        copy, move = (c.args[0] for c in run.call_args_list)
        self.assertEqual(["container", "cp", "/tmp/src"], copy[:3])
        staged = copy[3].split(":", 1)[1]
        self.assertTrue(staged.startswith("/tmp/bot-bottle-cp-"))
        self.assertEqual(["container", "exec", "--user", "root", "bot-bottle-dev-abc", "sh", "-c"],
                         move[:7])
        self.assertIn(f"rm -rf /home/node/src && mv {staged} /home/node/src", move[7])


if __name__ == "__main__":
    unittest.main()
