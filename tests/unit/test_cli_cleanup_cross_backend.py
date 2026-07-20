"""Unit: `cli.py cleanup` walks every available backend.

Asserts cmd_cleanup queries each available backend's `prepare_cleanup`,
combines the y/N output, and runs each backend's `cleanup` when the
operator confirms. Unavailable backends (e.g. macos-container on Linux)
are skipped so that `cleanup` never errors on a platform where a backend
is not installed. Mocks the backends and stdin."""

from __future__ import annotations

import unittest
from unittest.mock import patch, MagicMock

from bot_bottle.cli import cleanup as cmd


def _make_backend(empty: bool = True):
    backend = MagicMock()
    plan = MagicMock(empty=empty)
    backend.prepare_cleanup.return_value = plan
    backend.cleanup = MagicMock()
    return backend, plan


class TestCmdCleanup(unittest.TestCase):
    def test_iterates_every_available_backend(self):
        docker, docker_plan = _make_backend(empty=False)
        fc, fc_plan = _make_backend(empty=False)
        backends_by_name = {"docker": docker, "firecracker": fc}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "firecracker"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        docker.prepare_cleanup.assert_called_once()
        fc.prepare_cleanup.assert_called_once()
        docker.cleanup.assert_called_once_with(docker_plan)
        fc.cleanup.assert_called_once_with(fc_plan)

    def test_skips_unavailable_backends(self):
        # macos-container is not available on Linux — must be silently skipped.
        docker, docker_plan = _make_backend(empty=False)
        macos = MagicMock()
        backends_by_name = {"docker": docker}

        def _has(name: str) -> bool:
            return name == "docker"

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "macos-container"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", side_effect=_has,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        docker.prepare_cleanup.assert_called_once()
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
            side_effect=lambda name: backends_by_name[name],  # type: ignore
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
            side_effect=lambda name: backends_by_name[name],  # type: ignore
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
            side_effect=lambda name: backends_by_name[name],  # type: ignore
        ), patch.object(
            cmd, "has_backend", return_value=True,
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            cmd.cmd_cleanup([])
        docker.cleanup.assert_called_once_with(docker_plan)
        fc.cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
