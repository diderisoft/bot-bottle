"""Unit: `cli.py cleanup` walks every available backend.

Asserts cmd_cleanup queries each available backend's `prepare_cleanup`,
combines the y/N output, and runs each backend's `cleanup` when the
operator confirms. Unavailable backends (e.g. macos-container on Linux)
are skipped so that `cleanup` never errors on a platform where a backend
is not installed. Mocks the backends and stdin."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from bot_bottle import bottle_state
from bot_bottle.backend import EnumerationError
from bot_bottle.cli.commands import cleanup as cmd
from tests.unit import use_bottle_root


def _make_backend(empty: bool = True):
    backend = MagicMock()
    plan = MagicMock(empty=empty)
    plan.intersect.return_value = plan
    backend.prepare_cleanup.return_value = plan
    backend.enumerate_active.return_value = []
    backend.cleanup = MagicMock()
    return backend, plan


class _Root(unittest.TestCase):
    """A scratch bot-bottle root: no real host controller or state dirs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cleanup-test.")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(tmp.name) / ".bot-bottle"))


class TestCmdCleanup(_Root):
    def test_iterates_every_available_backend(self):
        docker, docker_plan = _make_backend(empty=False)
        fc, fc_plan = _make_backend(empty=False)
        backends_by_name = {"docker": docker, "firecracker": fc}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "firecracker"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda _settings, name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        self.assertEqual(2, docker.prepare_cleanup.call_count)
        self.assertEqual(2, fc.prepare_cleanup.call_count)
        docker.cleanup.assert_called_once_with(docker_plan)
        fc.cleanup.assert_called_once_with(fc_plan)

    def test_skips_unavailable_backends(self):
        # macos-container is not available on Linux — must be silently skipped.
        docker, docker_plan = _make_backend(empty=False)
        macos = MagicMock()
        backends_by_name = {"docker": docker}

        def _has(_settings: object, name: str) -> bool:
            return name == "docker"

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "macos-container"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda _settings, name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", side_effect=_has,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        self.assertEqual(2, docker.prepare_cleanup.call_count)
        docker.cleanup.assert_called_once_with(docker_plan)
        macos.prepare_cleanup.assert_not_called()

    def test_short_circuits_when_all_empty(self):
        docker, _ = _make_backend(empty=True)
        fc, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "firecracker": fc}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "firecracker"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda _settings, name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes",
        ) as prompt:
            self.assertEqual(0, cmd.cmd_cleanup([]))
        prompt.assert_not_called()
        docker.cleanup.assert_not_called()
        fc.cleanup.assert_not_called()

    def test_abort_at_prompt_runs_nothing(self):
        docker, _ = _make_backend(empty=False)
        fc, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "firecracker": fc}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "firecracker"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda _settings, name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=False,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))
        docker.cleanup.assert_not_called()
        fc.cleanup.assert_not_called()

    def test_skips_empty_plans_when_others_have_work(self):
        # docker has work, firecracker doesn't — only docker.cleanup
        # is called.
        docker, docker_plan = _make_backend(empty=False)
        fc, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "firecracker": fc}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "firecracker"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda _settings, name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            cmd.cmd_cleanup([])
        docker.cleanup.assert_called_once_with(docker_plan)
        fc.cleanup.assert_not_called()

    def test_executes_only_displayed_resources_still_current(self):
        backend = MagicMock()
        preview = MagicMock(empty=False)
        refreshed = MagicMock(empty=False)
        approved = MagicMock(empty=False)
        preview.intersect.return_value = approved
        backend.prepare_cleanup.side_effect = [preview, refreshed]
        backend.enumerate_active.return_value = []

        with patch.object(
            cmd, "known_backend_names", return_value=("firecracker",),
        ), patch.object(
            cmd, "get_bottle_backend", return_value=backend,
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        preview.intersect.assert_called_once_with(refreshed)
        backend.cleanup.assert_called_once_with(approved)



def _state(identity: str, backend: str) -> None:
    bottle_state.write_per_bottle_dockerfile(identity, "FROM x\n")
    bottle_state.write_metadata(bottle_state.BottleMetadata(
        identity=identity, agent_name="pi", cwd="", copy_cwd=False,
        started_at="2026-10-24T00:00:00Z", backend=backend))


class TestUnreachableAndOneBackend(_Root):
    """#647: a backend whose runtime is down is skipped, not fatal; and
    `--backend` limits the sweep."""

    def run_cleanup(self, argv: list[str], backends: dict[str, MagicMock]) -> int:
        def backend(_settings: object, name: str) -> MagicMock:
            return backends[name]

        with patch.object(cmd, "known_backend_names", return_value=tuple(sorted(backends))), \
             patch.object(cmd, "get_bottle_backend", side_effect=backend), \
             patch.object(cmd, "has_backend", return_value=True), \
             patch.object(cmd, "_prompt_yes", return_value=True):
            return cmd.cmd_cleanup(argv)

    def test_a_backend_that_cant_be_listed_is_skipped(self):
        docker = MagicMock()
        docker.enumerate_active.side_effect = EnumerationError("daemon not running")
        mac, mac_plan = _make_backend(empty=False)
        _state("dock-1", "docker")       # Docker's: might be live, so kept
        _state("mac-1", "macos-container")
        self.assertEqual(0, self.run_cleanup([], {"docker": docker, "macos-container": mac}))
        mac.cleanup.assert_called_once_with(mac_plan)
        docker.cleanup.assert_not_called()
        self.assertTrue(bottle_state.bottle_state_dir("dock-1").exists())
        self.assertFalse(bottle_state.bottle_state_dir("mac-1").exists())

    def test_one_backend_failing_still_cleans_the_others(self):
        docker, _ = _make_backend(empty=False)
        docker.cleanup.side_effect = cmd.CleanupError("compose down failed")
        mac, mac_plan = _make_backend(empty=False)
        with self.assertRaisesRegex(cmd.CleanupError, "docker: compose down failed"):
            self.run_cleanup([], {"docker": docker, "macos-container": mac})
        mac.cleanup.assert_called_once_with(mac_plan)

    def test_backend_flag_sweeps_only_that_backend(self):
        docker, _ = _make_backend(empty=False)
        mac, mac_plan = _make_backend(empty=False)
        mac.enumerate_active.return_value = []
        docker.enumerate_active.return_value = [MagicMock(slug="dock-live")]
        _state("dock-old", "docker")
        _state("dock-live", "docker")
        _state("mac-old", "macos-container")
        self.assertEqual(0, self.run_cleanup(["--backend", "macos-container"],
                                             {"docker": docker, "macos-container": mac}))
        mac.cleanup.assert_called_once_with(mac_plan)
        docker.prepare_cleanup.assert_not_called()
        docker.cleanup.assert_not_called()
        self.assertFalse(bottle_state.bottle_state_dir("mac-old").exists())
        self.assertTrue(bottle_state.bottle_state_dir("dock-old").exists())  # not swept

    def test_an_unknown_backend_is_refused(self):
        with patch.object(cmd, "known_backend_names", return_value=("docker",)), \
             self.assertRaisesRegex(SystemExit, "unknown backend 'nope'"):
            cmd.cmd_cleanup(["--backend=nope"])


if __name__ == "__main__":
    unittest.main()
