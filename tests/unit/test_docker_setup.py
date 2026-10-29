"""Unit: the docker backend's own setup/status logic (`install` and `doctor`
reach it; the firecracker path is exercised by test_firecracker_backend via
netpool)."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.docker import setup as docker_setup


class TestDockerSetupStatus(unittest.TestCase):
    def _ok(self):
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def test_setup_ok_when_docker_and_daemon_present(self):
        with patch.object(docker_setup.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(docker_setup, "_daemon_reachable", return_value=True), \
             patch.object(docker_setup._util, "runsc_available", return_value=True):
            self.assertEqual(0, docker_setup.setup())

    def test_setup_fails_when_docker_missing(self):
        with patch.object(docker_setup.shutil, "which", return_value=None):
            self.assertEqual(1, docker_setup.setup())

    def test_status_fails_when_daemon_unreachable(self):
        with patch.object(docker_setup.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(docker_setup, "_daemon_reachable", return_value=False), \
             patch.object(docker_setup._util, "runsc_available", return_value=False):
            self.assertEqual(1, docker_setup.status())

    def test_teardown_is_noop_success(self):
        self.assertEqual(0, docker_setup.teardown())


if __name__ == "__main__":
    unittest.main()
