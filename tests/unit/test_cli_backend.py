"""Unit: the generic `backend` CLI command.

`bot-bottle backend {setup,status} [--backend=NAME]` resolves a backend
and dispatches to its `setup()` / `status()` classmethods — no
backend-specific commands. Also covers the docker backend's own
setup/status logic (the firecracker path is exercised by
test_firecracker_backend via netpool)."""

from __future__ import annotations

import io
import subprocess
import unittest
from unittest.mock import ANY, MagicMock, patch

from bot_bottle.cli.commands import backend as cmd
from bot_bottle.backend.docker import setup as docker_setup


class TestCmdBackendDispatch(unittest.TestCase):
    def _fake_backend(self):
        b = MagicMock()
        b.setup.return_value = 0
        b.status.return_value = 3
        b.teardown.return_value = 0
        return b

    def test_setup_dispatches_to_backend(self):
        b = self._fake_backend()
        with patch.object(cmd, "get_bottle_backend", return_value=b) as gbb:
            rc = cmd.cmd_backend(["setup", "--backend", "firecracker"])
        gbb.assert_called_once_with(ANY, "firecracker")
        b.setup.assert_called_once_with()
        b.status.assert_not_called()
        self.assertEqual(0, rc)

    def test_setup_as_root_skips_the_users_notes(self):
        b = self._fake_backend()
        for euid, noted in ((501, True), (0, False)):
            with self.subTest(euid=euid), \
                    patch.object(cmd, "get_bottle_backend", return_value=b), \
                    patch.object(cmd.os, "geteuid", return_value=euid), \
                    patch.object(cmd.service_unit, "installed", return_value=False), \
                    patch.object(cmd, "tmux_version", return_value=None), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                cmd.cmd_backend(["setup", "--backend", "firecracker"])
            self.assertEqual(noted, "autostart" in err.getvalue())
            self.assertEqual(noted, "tmux" in err.getvalue())

    def test_status_dispatches_and_returns_backend_code(self):
        b = self._fake_backend()
        with patch.object(cmd, "get_bottle_backend", return_value=b):
            rc = cmd.cmd_backend(["status", "--backend", "docker"])
        b.status.assert_called_once_with()
        self.assertEqual(3, rc)

    def test_teardown_dispatches_to_backend(self):
        b = self._fake_backend()
        with patch.object(cmd, "get_bottle_backend", return_value=b):
            rc = cmd.cmd_backend(["teardown", "--backend", "firecracker"])
        b.teardown.assert_called_once_with()
        b.setup.assert_not_called()
        self.assertEqual(0, rc)

    def test_no_backend_flag_uses_host_default(self):
        b = self._fake_backend()
        with patch.object(cmd, "get_bottle_backend", return_value=b) as gbb:
            cmd.cmd_backend(["status"])
        gbb.assert_called_once_with(ANY, None)

    def test_unknown_action_exits(self):
        with self.assertRaises(SystemExit):
            cmd.cmd_backend(["bogus"])

    def test_unknown_backend_rejected_by_argparse(self):
        with self.assertRaises(SystemExit):
            cmd.cmd_backend(["status", "--backend", "nope"])


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
