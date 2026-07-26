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


if __name__ == "__main__":
    unittest.main()
