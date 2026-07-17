"""Unit: Freezer class hierarchy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle import bottle_state
from tests.unit import use_bottle_root
from bot_bottle.backend import ActiveAgent
from bot_bottle.backend.freeze import get_freezer
from bot_bottle.backend.docker.freezer import DockerFreezer
from bot_bottle.backend.macos_container.freezer import MacosContainerFreezer
from bot_bottle.backend.firecracker.freezer import FirecrackerFreezer


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="freezer-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")

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

    def test_firecracker(self):
        self.assertIsInstance(get_freezer("firecracker"), FirecrackerFreezer)

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

        def capture_freeze(agent: ActiveAgent) -> str:
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


class TestFirecrackerFreezer(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()
        # The freezer resolves the running VM's SSH key + config from
        # the per-bottle run dir under the firecracker cache; point that
        # at a temp dir so we can stage a fake live bottle.
        self._cache = tempfile.TemporaryDirectory(prefix="fc-freezer-cache.")
        self._cache_patch = patch(
            "bot_bottle.backend.firecracker.freezer.util.cache_dir",
            return_value=Path(self._cache.name),
        )
        self._cache_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._cache.cleanup()
        self._teardown_fake_home()

    def _write_meta(self, slug: str) -> None:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=slug, agent_name="dev", cwd="", copy_cwd=False,
            started_at="t", backend="firecracker",
        ))

    def _stage_run_dir(self, slug: str, guest_ip: str = "100.64.0.1") -> None:
        run_dir = Path(self._cache.name) / "run" / slug
        run_dir.mkdir(parents=True)
        (run_dir / "bottle_id_ed25519").write_text("KEY")
        (run_dir / "config.json").write_text(
            '{"boot-source": {"boot_args": '
            f'"console=ttyS0 ip={guest_ip}::100.64.0.0:255.255.255.254::eth0:off"}}}}'
        )

    def test_snapshots_running_vm_without_stopping(self):
        """Commit should tar the running guest rootfs over SSH into the
        committed-rootfs artifact (no Docker), not stop the VM."""
        slug = "dev-abc12"
        self._write_meta(slug)
        self._stage_run_dir(slug)
        freezer = FirecrackerFreezer()
        agent = _make_agent(slug, "firecracker")

        with patch("bot_bottle.backend.firecracker.freezer._commit_rootfs_via_ssh") as mock_commit, \
             patch("bot_bottle.backend.freeze.info"), \
             patch("bot_bottle.backend.firecracker.freezer.info"):
            freezer.commit(agent)

        tar_path = bottle_state.committed_rootfs_path(slug)
        self.assertEqual(1, mock_commit.call_count)
        # (private_key, guest_ip, tar_path) — guest_ip parsed from config.
        args = mock_commit.call_args.args
        self.assertEqual("100.64.0.1", args[1])
        self.assertEqual(tar_path, args[2])
        # The committed-image state records the artifact path; resume boots
        # from the tar rather than a Docker image.
        self.assertEqual(str(tar_path), bottle_state.read_committed_image(slug))
        self.assertTrue(bottle_state.is_preserved(slug))


if __name__ == "__main__":
    unittest.main()
