"""Unit: Freezer class hierarchy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle import supervise, bottle_state
from bot_bottle.backend import ActiveAgent
from bot_bottle.backend.freeze import get_freezer
from bot_bottle.backend.docker.freezer import DockerFreezer
from bot_bottle.backend.macos_container.freezer import MacosContainerFreezer
from bot_bottle.backend.smolmachines.freezer import SmolmachinesFreezer


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="freezer-test.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "bot_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


def _make_agent(slug: str, backend: str = "docker") -> ActiveAgent:
    return ActiveAgent(
        backend_name=backend,
        slug=slug,
        agent_name="dev",
        started_at="t",
        services=(),
    )


class TestGetFreezer(unittest.TestCase):
    def test_docker(self):
        self.assertIsInstance(get_freezer("docker"), DockerFreezer)

    def test_empty_backend_gives_docker(self):
        self.assertIsInstance(get_freezer(""), DockerFreezer)

    def test_macos_container(self):
        self.assertIsInstance(get_freezer("macos-container"), MacosContainerFreezer)

    def test_smolmachines(self):
        self.assertIsInstance(get_freezer("smolmachines"), SmolmachinesFreezer)

    def test_unknown_backend_dies(self):
        with patch("bot_bottle.backend.freeze.die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                get_freezer("unknown-backend")


class TestFreezerBaseCommit(_FakeHomeMixin, unittest.TestCase):
    """The base Freezer.commit() owns the shared post-freeze steps."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_writes_committed_image_and_marks_preserved(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))
        freezer = get_freezer("docker")
        agent = _make_agent(slug)

        with patch.object(freezer, "_freeze", return_value="bot-bottle-committed-dev-abc12:latest"), \
             patch("bot_bottle.backend.freeze.info"):
            freezer.commit(agent)

        self.assertEqual(
            "bot-bottle-committed-dev-abc12:latest",
            bottle_state.read_committed_image(slug),
        )
        self.assertTrue(bottle_state.is_preserved(slug))

    def test_commit_slug_passes_correct_slug_to_freeze(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))
        freezer = get_freezer("docker")
        captured = {}

        def capture_freeze(agent):
            captured["slug"] = agent.slug
            return "some-ref"

        with patch.object(freezer, "_freeze", side_effect=capture_freeze), \
             patch("bot_bottle.backend.freeze.info"):
            freezer.commit_slug(slug)

        self.assertEqual(slug, captured["slug"])


class TestDockerFreezer(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_commits_container_and_records_image(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="docker",
        ))
        freezer = DockerFreezer()
        agent = _make_agent(slug)

        with patch("bot_bottle.backend.docker.freezer.commit_container") as mock_commit, \
             patch("bot_bottle.backend.freeze.info"), \
             patch("bot_bottle.backend.docker.freezer.info"):
            freezer.commit(agent)

        mock_commit.assert_called_once_with(
            f"bot-bottle-{slug}",
            f"bot-bottle-committed-{slug}:latest",
        )
        self.assertEqual(
            f"bot-bottle-committed-{slug}:latest",
            bottle_state.read_committed_image(slug),
        )
        self.assertTrue(bottle_state.is_preserved(slug))


class TestMacosContainerFreezer(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _write_meta(self, slug: str) -> None:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="macos-container",
        ))

    def test_commits_running_container_without_stopping(self):
        """Commit should exec-tar the running container, not stop it."""
        slug = "dev-abc12"
        self._write_meta(slug)
        freezer = MacosContainerFreezer()
        agent = _make_agent(slug, "macos-container")

        with patch("bot_bottle.backend.macos_container.freezer.commit_container") as mock_commit, \
             patch("bot_bottle.backend.freeze.info"), \
             patch("bot_bottle.backend.macos_container.freezer.info"):
            freezer.commit(agent)

        mock_commit.assert_called_once_with(
            f"bot-bottle-{slug}",
            f"bot-bottle-committed-{slug}:latest",
        )
        self.assertEqual(
            f"bot-bottle-committed-{slug}:latest",
            bottle_state.read_committed_image(slug),
        )
        self.assertTrue(bottle_state.is_preserved(slug))


class TestSmolmachinesFreezer(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_packs_vm_and_records_artifact(self):
        slug = "dev-abc12"
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="smolmachines",
        ))
        freezer = SmolmachinesFreezer()
        agent = _make_agent(slug, "smolmachines")

        with patch("bot_bottle.backend.smolmachines.freezer.pack_create_from_vm") as mock_pack, \
             patch("bot_bottle.backend.freeze.info"), \
             patch("bot_bottle.backend.smolmachines.freezer.info"):
            freezer.commit(agent)

        expected_output = bottle_state.bottle_state_dir(slug) / "committed-smolmachine"
        mock_pack.assert_called_once_with(f"bot-bottle-{slug}", expected_output)
        expected_artifact = str(expected_output.with_name("committed-smolmachine.smolmachine"))
        self.assertEqual(expected_artifact, bottle_state.read_committed_image(slug))
        self.assertTrue(bottle_state.is_preserved(slug))


if __name__ == "__main__":
    unittest.main()
