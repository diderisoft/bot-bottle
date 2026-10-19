"""Unit: docker compose lifecycle helpers (PRD 0018).

The compose *spec* is built by `consolidated_compose.py`; these
tests cover the I/O-side helpers in `compose.py` — the slug ↔
project mapping and `docker compose ls` enumeration.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from bot_bottle.backend.docker.compose import (
    COMPOSE_PROJECT_PREFIX,
    compose_project_name,
    list_active_slugs,
    list_compose_projects,
    slug_from_compose_project,
)
from bot_bottle.backend import EnumerationError


class TestProjectNaming(unittest.TestCase):
    """The slug ↔ compose-project mapping is the contract dashboard,
    cleanup, and launch all rely on. Lock it down."""

    def test_compose_project_name_is_prefix_plus_slug(self):
        self.assertEqual(
            f"{COMPOSE_PROJECT_PREFIX}myagent-abc12",
            compose_project_name("myagent-abc12"),
        )

    def test_slug_from_compose_project_is_inverse(self):
        self.assertEqual(
            "myagent-abc12",
            slug_from_compose_project(f"{COMPOSE_PROJECT_PREFIX}myagent-abc12"),
        )

    def test_slug_from_unrelated_project_returns_empty(self):
        # Defends against `docker compose ls` including non-bottle
        # projects on a host with other compose setups.
        self.assertEqual("", slug_from_compose_project("other-project"))


class TestComposeProjectListing(unittest.TestCase):
    def setUp(self):
        # The daemon answers unless a test says otherwise; the real probe would
        # make these tests depend on the host's docker.
        probe = mock.patch("bot_bottle.backend.docker.compose.daemon_running",
                           return_value=True)
        self.daemon_running = probe.start()
        self.addCleanup(probe.stop)

    def test_compose_ls_empty_when_docker_unusable(self):
        # Missing is the obvious case; present-but-not-executable raises
        # PermissionError instead, which must not escape as a crash.
        for exc in (FileNotFoundError, PermissionError(13, "Permission denied", "docker")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch(
                    "bot_bottle.backend.docker.compose.subprocess.run",
                    side_effect=exc,
                ):
                    self.assertEqual([], list_compose_projects())

    def test_compose_ls_error_warns_by_default(self):
        with (
            mock.patch(
                "bot_bottle.backend.docker.compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["docker"], returncode=1, stdout="", stderr="no daemon",
                ),
            ),
            mock.patch("bot_bottle.backend.docker.compose.warn") as warn,
        ):
            self.assertEqual([], list_compose_projects())
        warn.assert_called_once_with("docker compose ls failed: no daemon")

    def test_compose_ls_error_can_be_quiet_for_dashboard_polling(self):
        with (
            mock.patch(
                "bot_bottle.backend.docker.compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["docker"], returncode=1, stdout="", stderr="no daemon",
                ),
            ),
            mock.patch("bot_bottle.backend.docker.compose.warn") as warn,
        ):
            self.assertEqual([], list_active_slugs(warn_on_error=False))
        warn.assert_not_called()

    def test_compose_ls_error_can_be_raised_for_enumeration(self):
        with mock.patch(
            "bot_bottle.backend.docker.compose.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["docker"], returncode=1, stdout="", stderr="no daemon",
            ),
        ):
            with self.assertRaisesRegex(EnumerationError, "no daemon"):
                list_active_slugs(
                    warn_on_error=False,
                    raise_on_error=True,
                )

    def test_stopped_daemon_is_an_authoritative_empty_result(self):
        # Docker installed but not running must not break cross-backend
        # enumeration (e.g. `start` on macOS): nothing runs on a stopped daemon.
        # Decided by probing the daemon, not by the failure's wording.
        self.daemon_running.return_value = False
        with mock.patch(
            "bot_bottle.backend.docker.compose.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["docker"], returncode=1, stdout="", stderr="any wording",
            ),
        ):
            self.assertEqual([], list_active_slugs(warn_on_error=False, raise_on_error=True))


class TestDaemonRunning(unittest.TestCase):
    _RUN = "bot_bottle.backend.docker.util.run_docker"

    def test_answers_from_the_server_version_probe(self):
        from bot_bottle.backend.docker.util import daemon_running
        up = subprocess.CompletedProcess(["docker"], 0, "28.0.1\n", "")
        down = subprocess.CompletedProcess(["docker"], 1, "", "Cannot connect")
        with mock.patch(self._RUN, return_value=up) as run:
            self.assertTrue(daemon_running())
        self.assertEqual(["docker", "version", "--format", "{{.Server.Version}}"],
                         run.call_args.args[0])
        with mock.patch(self._RUN, return_value=down):
            self.assertFalse(daemon_running())

    def test_missing_cli_is_not_running_but_a_hang_is_not_assumed_down(self):
        from bot_bottle.backend.docker.util import daemon_running
        with mock.patch(self._RUN, side_effect=FileNotFoundError):
            self.assertFalse(daemon_running())
        with mock.patch(self._RUN, side_effect=subprocess.TimeoutExpired("docker", 10)):
            self.assertTrue(daemon_running())


if __name__ == "__main__":
    unittest.main()
