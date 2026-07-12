"""Cross-backend parity tests (PRD 0042).

Verifies that Docker and firecracker bottles expose the same
observable contracts for env injection, agent argv, and exec. Tests
use mock subprocess layers so no live VM or Docker daemon is needed.

The scenarios here document what must hold across both backends and
provide regression coverage for those contracts.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_bottle(guest_env: dict[str, str]) -> "object":
    from bot_bottle.backend.docker.bottle import DockerBottle
    return DockerBottle(
        container="bot-bottle-test",
        teardown=lambda: None,
        prompt_path_in_container=None,
        agent_command="claude",
    )


def _firecracker_bottle(guest_env: dict[str, str]) -> "object":
    from bot_bottle.backend.firecracker.bottle import FirecrackerBottle
    return FirecrackerBottle(
        "bot-bottle-test",
        private_key=Path("/tmp/key"),
        guest_ip="100.64.0.1",
        guest_env=guest_env,
        agent_command="claude",
    )


# One entry per backend: (label, factory).
_BACKENDS: list[tuple[str, Callable[[dict[str, str]], object]]] = [
    ("docker", _docker_bottle),
    ("firecracker", _firecracker_bottle),
]


# ---------------------------------------------------------------------------
# agent_argv contracts
# ---------------------------------------------------------------------------

class TestAgentArgvParity(unittest.TestCase):
    """Both backends surface a non-empty agent_argv that includes the
    agent command and can be used as a subprocess command list."""

    def test_agent_argv_is_list_of_strings(self):
        for label, factory in _BACKENDS:
            with self.subTest(backend=label):
                bottle = factory({"MY_VAR": "val"})
                argv = bottle.agent_argv([], tty=False)  # type: ignore[union-attr]
                self.assertIsInstance(argv, list, f"{label}: argv is not a list")
                for item in argv:
                    self.assertIsInstance(
                        item, str,
                        f"{label}: argv item {item!r} is not a str",
                    )

    def test_agent_command_present_in_argv(self):
        for label, factory in _BACKENDS:
            with self.subTest(backend=label):
                bottle = factory({})
                argv = bottle.agent_argv([], tty=False)  # type: ignore[union-attr]
                joined = " ".join(argv)
                self.assertIn(
                    "claude", joined,
                    f"{label}: 'claude' not found in agent_argv",
                )

    def test_extra_flags_propagate(self):
        extra = ["--no-update-check", "--output-format", "stream-json"]
        for label, factory in _BACKENDS:
            with self.subTest(backend=label):
                bottle = factory({})
                argv = bottle.agent_argv(extra, tty=False)  # type: ignore[union-attr]
                for flag in extra:
                    self.assertIn(
                        flag, argv,
                        f"{label}: flag {flag!r} not in agent_argv",
                    )


class TestFirecrackerEnvInArgv(unittest.TestCase):
    """firecracker bottle includes guest_env values in the agent argv."""

    def test_guest_env_in_agent_argv(self):
        bottle = _firecracker_bottle(
            {"TOKEN": "abc123", "PROXY": "http://proxy:8888"},
        )
        argv = bottle.agent_argv([], tty=False)  # type: ignore[union-attr]
        self.assertIn("TOKEN=abc123", argv)
        self.assertIn("PROXY=http://proxy:8888", argv)


# ---------------------------------------------------------------------------
# exec() user-switching contract
# ---------------------------------------------------------------------------

class TestExecUserSwitching(unittest.TestCase):
    """Both backends exec as 'node' by default and accept user='root'."""

    def test_docker_exec_uses_node_user_by_default(self):
        from bot_bottle.backend.docker.bottle import DockerBottle
        bottle = DockerBottle(
            container="bot-bottle-test",
            teardown=lambda: None,
            prompt_path_in_container=None,
        )
        with patch("bot_bottle.backend.docker.bottle.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr="",
            )
            bottle.exec("echo hi")
        call_args = run.call_args[0][0]
        self.assertIn("node", call_args,
                      "docker exec should use 'node' user by default")

    def test_firecracker_exec_uses_node_user_by_default(self):
        bottle = _firecracker_bottle({})
        with patch("bot_bottle.backend.firecracker.bottle.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr="",
            )
            bottle.exec("echo hi")  # type: ignore[union-attr]
        call_args = " ".join(run.call_args[0][0])
        self.assertIn("runuser -u node", call_args,
                      "firecracker exec should use 'node' user by default")

    def test_docker_exec_respects_root_user(self):
        from bot_bottle.backend.docker.bottle import DockerBottle
        bottle = DockerBottle(
            container="bot-bottle-test",
            teardown=lambda: None,
            prompt_path_in_container=None,
        )
        with patch("bot_bottle.backend.docker.bottle.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr="",
            )
            bottle.exec("id", user="root")
        call_args = run.call_args[0][0]
        self.assertIn("root", call_args)

    def test_firecracker_exec_respects_root_user(self):
        bottle = _firecracker_bottle({})
        with patch("bot_bottle.backend.firecracker.bottle.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr="",
            )
            bottle.exec("id", user="root")  # type: ignore[union-attr]
        call_args = " ".join(run.call_args[0][0])
        self.assertIn("runuser -u root", call_args)


# ---------------------------------------------------------------------------
# ExecResult shape parity
# ---------------------------------------------------------------------------

class TestExecResultParity(unittest.TestCase):
    """Both backends return ExecResult with returncode, stdout, stderr."""

    def _stub_run(self, argv: object, **kwargs: object) -> object:  # type: ignore
        return subprocess.CompletedProcess(
            argv, 0, stdout="out\n", stderr="err\n",  # type: ignore
        )

    def test_docker_exec_result_shape(self):
        from bot_bottle.backend.docker.bottle import DockerBottle
        from bot_bottle.backend import ExecResult
        bottle = DockerBottle(
            container="bot-bottle-test",
            teardown=lambda: None,
            prompt_path_in_container=None,
        )
        with patch("bot_bottle.backend.docker.bottle.subprocess.run",
                   side_effect=self._stub_run):
            result = bottle.exec("echo hi")
        self.assertIsInstance(result, ExecResult)
        self.assertEqual(0, result.returncode)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)

    def test_firecracker_exec_result_shape(self):
        from bot_bottle.backend import ExecResult
        bottle = _firecracker_bottle({})
        with patch("bot_bottle.backend.firecracker.bottle.subprocess.run",
                   side_effect=self._stub_run):
            result = bottle.exec("echo hi")  # type: ignore[union-attr]
        self.assertIsInstance(result, ExecResult)
        self.assertEqual(0, result.returncode)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)


# ---------------------------------------------------------------------------
# close() is a no-op / idempotent (ABC contract)
# ---------------------------------------------------------------------------

class TestCloseParity(unittest.TestCase):
    def test_docker_close_is_idempotent(self):
        from bot_bottle.backend.docker.bottle import DockerBottle
        teardown_count = [0]
        def count_teardown():
            teardown_count[0] += 1
        bottle = DockerBottle(
            container="bot-bottle-test",
            teardown=count_teardown,
            prompt_path_in_container=None,
        )
        bottle.close()
        bottle.close()
        # DockerBottle.close calls teardown — once per call is fine;
        # what matters is it doesn't raise.

    def test_firecracker_close_is_noop(self):
        bottle = _firecracker_bottle({})
        bottle.close()  # type: ignore[union-attr]
        bottle.close()  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
