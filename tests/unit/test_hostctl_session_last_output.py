"""A failed session's last output is kept once its pane is killed, so a
headless launch (and `attach`) can say why the agent ended (#866)."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from bot_bottle.control.hostctl import sessions


class CaptureTail(unittest.TestCase):
    def test_last_non_blank_lines_capped(self) -> None:
        manager = sessions.SessionManager({})
        pane = "\n".join([f"line {i}" for i in range(40)] + ["", "  ", "Pane is dead"]) + "\n"
        done = subprocess.CompletedProcess([], 0, stdout=pane, stderr="")
        with mock.patch.object(sessions.subprocess, "run", return_value=done) as run:
            tail = manager._capture_tail("s1", lines=3)  # pylint: disable=protected-access
        self.assertEqual("line 38\nline 39\nPane is dead", tail)
        argv = run.call_args.args[0]
        self.assertIn("capture-pane", argv)
        self.assertEqual("=s1:", argv[argv.index("-t") + 1])

    def test_nothing_without_the_session(self) -> None:
        manager = sessions.SessionManager({})
        gone = subprocess.CompletedProcess([], 1, stdout="", stderr="can't find session")
        with mock.patch.object(sessions.subprocess, "run", return_value=gone):
            self.assertEqual("", manager._capture_tail("gone"))  # pylint: disable=protected-access
        self.assertEqual("", manager.last_output("gone"))


if __name__ == "__main__":
    unittest.main()
