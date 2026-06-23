"""Unit: cli.py commit command."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.cli.commit import (
    cmd_commit,
    _agent_container_name,
    _committed_image_tag,
    _committed_smolmachine_artifact,
    _committed_smolmachine_output,
)
from bot_bottle import supervise
from bot_bottle import bottle_state


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cli-commit-test.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "bot_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestCommitHelpers(unittest.TestCase):
    def test_committed_image_tag(self):
        self.assertEqual(
            "bot-bottle-committed-dev-abc12:latest",
            _committed_image_tag("dev-abc12"),
        )

    def test_agent_container_name(self):
        self.assertEqual(
            "bot-bottle-dev-abc12",
            _agent_container_name("dev-abc12"),
        )

    def test_committed_smolmachine_paths(self):
        output = _committed_smolmachine_output("dev-abc12")
        artifact = _committed_smolmachine_artifact("dev-abc12")
        self.assertTrue(str(output).endswith(
            "/.bot-bottle/state/dev-abc12/committed-smolmachine"
        ))
        self.assertTrue(str(artifact).endswith(
            "/.bot-bottle/state/dev-abc12/committed-smolmachine.smolmachine"
        ))


class TestCmdCommitSlugArg(_FakeHomeMixin, unittest.TestCase):
    """cmd_commit with an explicit slug bypasses the TUI picker."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_commits_docker_bottle(self):
        slug = "dev-abc12"
        # Write metadata saying this is a docker bottle.
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))

        with patch(
            "bot_bottle.cli.commit.docker_commit_container",
        ) as mock_commit, patch(
            "bot_bottle.cli.commit.info",
        ):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_commit.assert_called_once_with(
            f"bot-bottle-{slug}",
            f"bot-bottle-committed-{slug}:latest",
        )

    def test_writes_committed_image_to_state(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))

        with patch("bot_bottle.cli.commit.docker_commit_container"), \
             patch("bot_bottle.cli.commit.info"):
            cmd_commit([slug])

        self.assertEqual(
            f"bot-bottle-committed-{slug}:latest",
            bottle_state.read_committed_image(slug),
        )

    def test_marks_bottle_preserved(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))

        with patch("bot_bottle.cli.commit.docker_commit_container"), \
             patch("bot_bottle.cli.commit.info"):
            cmd_commit([slug])

        self.assertTrue(bottle_state.is_preserved(slug))

    def test_empty_backend_treated_as_docker(self):
        """Old state dirs without a backend field should be treated as docker."""
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="",
        ))

        with patch("bot_bottle.cli.commit.docker_commit_container") as mock_commit, \
             patch("bot_bottle.cli.commit.info"):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_commit.assert_called_once()

    def test_commits_macos_container_bottle(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="macos-container",
        ))

        with patch(
            "bot_bottle.cli.commit.macos_commit_container",
        ) as mock_commit, patch(
            "bot_bottle.cli.commit.info",
        ), patch(
            "bot_bottle.cli.commit.macos_container_is_running", return_value=False,
        ):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_commit.assert_called_once_with(
            f"bot-bottle-{slug}",
            f"bot-bottle-committed-{slug}:latest",
        )

    def test_running_macos_container_stops_then_commits_on_yes(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="macos-container",
        ))

        with patch(
            "bot_bottle.cli.commit.macos_container_is_running", return_value=True,
        ), patch(
            "bot_bottle.cli.commit.read_tty_line", return_value="y",
        ), patch(
            "bot_bottle.cli.commit.macos_stop_container",
        ) as mock_stop, patch(
            "bot_bottle.cli.commit.macos_commit_container",
        ) as mock_commit, patch(
            "bot_bottle.cli.commit.info",
        ):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_stop.assert_called_once_with(f"bot-bottle-{slug}")
        mock_commit.assert_called_once_with(
            f"bot-bottle-{slug}",
            f"bot-bottle-committed-{slug}:latest",
        )

    def test_running_macos_container_aborts_on_no(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="macos-container",
        ))

        with patch(
            "bot_bottle.cli.commit.macos_container_is_running", return_value=True,
        ), patch(
            "bot_bottle.cli.commit.read_tty_line", return_value="n",
        ), patch(
            "bot_bottle.cli.commit.macos_stop_container",
        ) as mock_stop, patch(
            "bot_bottle.cli.commit.macos_commit_container",
        ) as mock_commit, patch(
            "bot_bottle.cli.commit.info",
        ):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_stop.assert_not_called()
        mock_commit.assert_not_called()


class TestCmdCommitSmolmachinesBackend(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_packs_smolmachines_bottle(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="smolmachines",
        ))

        with patch(
            "bot_bottle.cli.commit.pack_create_from_vm",
        ) as mock_pack, patch(
            "bot_bottle.cli.commit.info",
        ):
            rc = cmd_commit([slug])

        self.assertEqual(0, rc)
        mock_pack.assert_called_once_with(
            f"bot-bottle-{slug}",
            _committed_smolmachine_output(slug),
        )
        self.assertEqual(
            str(_committed_smolmachine_artifact(slug)),
            bottle_state.read_committed_image(slug),
        )
        self.assertTrue(bottle_state.is_preserved(slug))


class TestCmdCommitUnsupportedBackend(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()


class TestCmdCommitNoActiveBottles(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_dies_when_no_active_bottles_and_no_slug(self):
        with patch(
            "bot_bottle.cli.commit.enumerate_active_agents", return_value=[],
        ), patch(
            "bot_bottle.cli.commit.die", side_effect=SystemExit("die"),
        ) as mock_die:
            with self.assertRaises(SystemExit):
                cmd_commit([])

        mock_die.assert_called_once()

    def test_returns_zero_when_picker_cancelled(self):
        active = MagicMock()
        active.slug = "dev-abc12"
        with patch(
            "bot_bottle.cli.commit.enumerate_active_agents", return_value=[active],
        ), patch(
            "bot_bottle.cli.commit.tui.filter_select", return_value=None,
        ):
            rc = cmd_commit([])

        self.assertEqual(0, rc)


if __name__ == "__main__":
    unittest.main()
