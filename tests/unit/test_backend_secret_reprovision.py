"""Backend-agnostic and backend-specific encrypted-secret recovery tests."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker import consolidated_launch as fc


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TestFirecrackerReprovision(unittest.TestCase):
    def test_persists_key_over_stdin_not_argv(self) -> None:
        with patch.object(fc.util, "ssh_base_argv", return_value=["ssh", "guest"]), \
             patch.object(fc.subprocess, "run", return_value=_proc()) as run:
            fc.persist_env_var_secret(Path("/key"), "10.0.0.1", "super-secret")
        self.assertEqual("super-secret", run.call_args.kwargs["input"])
        self.assertNotIn("super-secret", " ".join(run.call_args.args[0]))

    def test_persist_failure_is_fatal_to_launch(self) -> None:
        with patch.object(fc.util, "ssh_base_argv", return_value=["ssh", "guest"]), \
             patch.object(fc.subprocess, "run", return_value=_proc(1, stderr="denied")):
            with self.assertRaisesRegex(fc.ConsolidatedLaunchError, "denied"):
                fc.persist_env_var_secret(Path("/key"), "10.0.0.1", "secret")


if __name__ == "__main__":
    unittest.main()
