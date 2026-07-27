"""Unit: how the CLI tells users to re-run it under sudo.

`sudo bot-bottle …` is wrong for the users who followed the documented
install: sudo's secure_path excludes ~/.local/bin, where both pipx and
install.sh put the entry point. These lock in the absolute-path form.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import invocation


class TestSelfPath(unittest.TestCase):
    def test_absolute_argv0_is_used_as_is(self):
        with mock.patch.object(invocation.sys, "argv", ["/opt/venv/bin/bot-bottle"]):
            self.assertEqual("/opt/venv/bin/bot-bottle", invocation.self_path())

    def test_bare_name_is_resolved_through_path(self):
        # The case that matters: invoked as `bot-bottle`, installed in a
        # directory sudo would drop.
        with tempfile.TemporaryDirectory() as d:
            entry = Path(d, "bot-bottle")
            entry.write_text("#!/bin/sh\n")
            entry.chmod(0o755)
            with mock.patch.object(invocation.sys, "argv", ["bot-bottle"]), \
                 mock.patch.dict(os.environ, {"PATH": d}):
                self.assertEqual(str(entry), invocation.self_path())

    def test_relative_path_is_made_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            entry = Path(d, "bot-bottle")
            entry.write_text("#!/bin/sh\n")
            entry.chmod(0o755)
            cwd = os.getcwd()
            try:
                os.chdir(d)
                with mock.patch.object(invocation.sys, "argv", ["./bot-bottle"]):
                    self.assertTrue(os.path.isabs(invocation.self_path()))
            finally:
                os.chdir(cwd)

    def test_unresolvable_entry_point_falls_back_to_the_name(self):
        # `python -m`-style invocation, or an argv[0] that no longer exists.
        # A slightly wrong hint beats a traceback raised while reporting some
        # unrelated problem.
        with mock.patch.object(invocation.sys, "argv", ["/nonexistent/gone"]), \
             mock.patch.object(invocation.shutil, "which", return_value=None):
            self.assertEqual("/nonexistent/gone", invocation.self_path())
        with mock.patch.object(invocation.sys, "argv", [""]), \
             mock.patch.object(invocation.shutil, "which", return_value=None):
            self.assertEqual("bot-bottle", invocation.self_path())


class TestSudoCommand(unittest.TestCase):
    def test_names_an_absolute_path_not_the_bare_command(self):
        with mock.patch.object(invocation, "self_path",
                               return_value="/home/u/.local/bin/bot-bottle"):
            cmd = invocation.sudo_command("backend", "setup", "--backend=firecracker")
        self.assertEqual(
            "sudo /home/u/.local/bin/bot-bottle backend setup --backend=firecracker",
            cmd,
        )
        # The regression this exists to prevent.
        self.assertNotIn("sudo bot-bottle", cmd)


class TestFirecrackerSetupUsesIt(unittest.TestCase):
    def test_root_reinvocation_hint_names_an_absolute_path(self):
        # The message that prompted all this. Drive the real code path rather
        # than scanning the source, which would also match the comment
        # explaining why the bare form is wrong.
        from bot_bottle.backend.firecracker import setup as fc_setup

        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(fc_setup.os, "geteuid", return_value=501), \
             mock.patch.object(fc_setup.invocation, "self_path",
                               return_value="/home/u/.local/bin/bot-bottle"), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            fc_setup._setup_systemd()

        printed = err.getvalue()
        self.assertIn(
            "sudo /home/u/.local/bin/bot-bottle backend setup --backend=firecracker",
            printed,
        )
        self.assertNotIn("sudo bot-bottle", printed)


if __name__ == "__main__":
    unittest.main()
