"""Unit: Apple Container bottle command construction."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.backend.macos_container.bottle import MacosContainerBottle


class TestMacosContainerBottle(unittest.TestCase):
    def test_agent_argv_uses_container_exec(self):
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_command="codex",
        )
        self.assertEqual(
            [
                "container", "exec", "--interactive", "--tty",
                "bot-bottle-dev-abc", "codex", "run",
            ],
            bottle.agent_argv(["run"]),
        )

    def test_agent_argv_includes_workdir(self):
        bottle = MacosContainerBottle(
            "bot-bottle-dev-abc",
            lambda: None,
            None,
            agent_workdir="/home/node/workspace",
        )
        self.assertEqual(
            [
                "container", "exec", "--interactive", "--tty",
                "--workdir", "/home/node/workspace",
                "bot-bottle-dev-abc", "claude",
            ],
            bottle.agent_argv([]),
        )

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
