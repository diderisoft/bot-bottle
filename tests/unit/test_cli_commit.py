"""Unit: cli.py commit command."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.cli.commands.commit import cmd_commit
from tests.unit import use_bottle_root
from bot_bottle import bottle_state
from bot_bottle.backend.freeze import CommitCancelled


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cli-commit-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestCmdCommitSlugArg(_FakeHomeMixin, unittest.TestCase):
    """cmd_commit with an explicit slug delegates to get_freezer."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _write_meta(self, slug: str, backend: str) -> None:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend=backend,
        ))

    def test_commits_docker_bottle(self):
        slug = "dev-abc12"
        self._write_meta(slug, "docker")

        with patch("bot_bottle.cli.commands.commit.get_freezer") as mock_gf:
            mock_freezer = MagicMock()
            mock_gf.return_value = mock_freezer
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_gf.assert_called_once_with("docker")
        mock_freezer.commit_slug.assert_called_once_with(slug)

    def test_empty_backend_passed_to_get_freezer(self):
        """Old state dirs without a backend field pass '' to get_freezer."""
        slug = "dev-abc12"
        self._write_meta(slug, "")

        with patch("bot_bottle.cli.commands.commit.get_freezer") as mock_gf:
            mock_freezer = MagicMock()
            mock_gf.return_value = mock_freezer
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_gf.assert_called_once_with("")

    def test_commits_macos_container_bottle(self):
        slug = "dev-abc12"
        self._write_meta(slug, "macos-container")

        with patch("bot_bottle.cli.commands.commit.get_freezer") as mock_gf:
            mock_freezer = MagicMock()
            mock_gf.return_value = mock_freezer
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_gf.assert_called_once_with("macos-container")
        mock_freezer.commit_slug.assert_called_once_with(slug)

    def test_commits_firecracker_bottle(self):
        slug = "dev-abc12"
        self._write_meta(slug, "firecracker")

        with patch("bot_bottle.cli.commands.commit.get_freezer") as mock_gf:
            mock_freezer = MagicMock()
            mock_gf.return_value = mock_freezer
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_gf.assert_called_once_with("firecracker")

    def test_returns_zero_on_commit_cancelled(self):
        slug = "dev-abc12"
        self._write_meta(slug, "macos-container")

        with patch("bot_bottle.cli.commands.commit.get_freezer") as mock_gf:
            mock_freezer = MagicMock()
            mock_freezer.commit_slug.side_effect = CommitCancelled
            mock_gf.return_value = mock_freezer
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)


class TestCmdCommitNoActiveBottles(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_dies_when_no_active_bottles_and_no_slug(self):
        with patch(
            "bot_bottle.cli.commands.commit.enumerate_active_agents", return_value=[],
        ), patch(
            "bot_bottle.cli.commands.commit.die", side_effect=SystemExit("die"),
        ) as mock_die:
            with self.assertRaises(SystemExit):
                cmd_commit([])

        mock_die.assert_called_once()

    def test_returns_zero_when_picker_cancelled(self):
        active = MagicMock()
        active.slug = "dev-abc12"
        with patch(
            "bot_bottle.cli.commands.commit.enumerate_active_agents", return_value=[active],
        ), patch(
            "bot_bottle.cli.commands.commit.tui.filter_select", return_value=None,
        ):
            rc = cmd_commit([])

        self.assertEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
