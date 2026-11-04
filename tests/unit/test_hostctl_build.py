"""Unit: spotting a host controller that predates an upgrade (#851)."""

from __future__ import annotations

import unittest
from unittest import mock

from bot_bottle.control.hostctl import build, runner
from bot_bottle.control.hostctl.runner import HostControlState


class TestRestartNeeded(unittest.TestCase):
    def test_the_daemon_compares_the_code_on_disk_with_what_it_started_from(self) -> None:
        with mock.patch.object(build, "build_id", return_value="new"), \
             mock.patch.object(build, "current_build_id", return_value="old"):
            self.assertTrue(build.code_changed())
        with mock.patch.object(build, "build_id", return_value="same"), \
             mock.patch.object(build, "current_build_id", return_value="same"):
            self.assertFalse(build.code_changed())

    def test_the_cli_compares_the_running_controller_with_the_installed_code(self) -> None:
        state = HostControlState(pid=7, port=1234, build_id="old")
        with mock.patch.object(runner, "current_build_id", return_value="new"):
            self.assertTrue(runner.restart_needed(state))
        with mock.patch.object(runner, "current_build_id", return_value="old"):
            self.assertFalse(runner.restart_needed(state))

    def test_the_message_names_the_fix_and_that_bottles_survive_it(self) -> None:
        self.assertIn("`bot-bottle up --restart`", build.RESTART_NEEDED)
        self.assertIn("running bottles keep running", build.RESTART_NEEDED)


if __name__ == "__main__":
    unittest.main()
