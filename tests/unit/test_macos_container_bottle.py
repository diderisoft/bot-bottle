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
        with patch.dict(bottle_mod.os.environ, {"TERM": "xterm-256color"}, clear=False):
            argv = bottle.agent_argv(["run"])
        self.assertEqual(
            [
                sys.executable, _PTY_FORWARD_SCRIPT, "--",
                "container", "exec", "--interactive", "--tty",
                "--env", "TERM=xterm-256color",
                "bot-bottle-dev-abc", "codex", "run",
            ],
            argv,
        )

    def test_agent_argv_includes_workdir(self):
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_workdir="/home/node/workspace",
        )
        with patch.dict(bottle_mod.os.environ, {"TERM": "xterm-256color"}, clear=False):
            argv = bottle.agent_argv([])
        self.assertEqual(
            [
                sys.executable, _PTY_FORWARD_SCRIPT, "--",
                "container", "exec", "--interactive", "--tty",
                "--env", "TERM=xterm-256color",
                "--workdir", "/home/node/workspace",
                "bot-bottle-dev-abc", "claude",
            ],
            argv,
        )

    def test_agent_argv_uses_host_term_value(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch.dict(bottle_mod.os.environ, {"TERM": "screen-256color"}, clear=False):
            argv = bottle.agent_argv([])
        self.assertIn("TERM=screen-256color", argv)

    def test_agent_argv_term_falls_back_to_xterm_256color(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        env_without_term = {k: v for k, v in bottle_mod.os.environ.items() if k != "TERM"}
        with patch.object(bottle_mod.os, "environ", env_without_term):
            argv = bottle.agent_argv([])
        self.assertIn("TERM=xterm-256color", argv)

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

    def test_cp_in_uses_container_cp(self):
        bottle = MacosContainerBottle("bot-bottle-dev-abc", lambda: None, None)
        with patch("bot_bottle.backend.macos_container.bottle.subprocess.run") as run:
            bottle.cp_in("/tmp/src", "/home/node/src")
        self.assertEqual(
            ["container", "cp", "/tmp/src", "bot-bottle-dev-abc:/home/node/src"],
            run.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
