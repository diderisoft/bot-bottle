"""Unit: host-side `python -m` spawns import the installed package, never one
in the working directory (#706)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.control.hostctl import runner, sessions


class IsolatedImports(unittest.TestCase):
    def test_a_bot_bottle_package_in_the_cwd_is_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            planted = Path(cwd) / "bot_bottle"
            planted.mkdir()
            (planted / "__init__.py").write_text("raise SystemExit(42)\n")
            # A module with no side effects, run the way the tmux wrappers run.
            command = sessions._python_command("bot_bottle.contract.blocks")  # pylint: disable=protected-access
            done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                                  check=False)
        self.assertEqual(0, done.returncode, done.stderr)

    def test_the_daemon_is_started_with_safe_path(self) -> None:
        with mock.patch.object(runner.os, "posix_spawn", return_value=1) as spawn, \
                mock.patch.object(runner, "_await_state"), \
                mock.patch("bot_bottle.control.hostctl.service_unit.installed",
                           return_value=False), \
                tempfile.TemporaryDirectory() as root, \
                mock.patch.object(runner, "state_dir", return_value=Path(root)):
            runner._start(1.0)  # pylint: disable=protected-access
        argv = spawn.call_args.args[1]
        self.assertEqual([sys.executable, "-P", "-m", "bot_bottle.control.hostctl"], argv)

    def test_the_daemon_leaves_the_callers_directory(self) -> None:
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(runner, "state_dir", return_value=Path(root) / "hostctl"):
            runner.leave_callers_directory()
            self.assertEqual((Path(root) / "hostctl").resolve(), Path.cwd().resolve())


if __name__ == "__main__":
    unittest.main()
