"""Unit: `cli.py cleanup` walks every backend (issue follow-up).

Asserts cmd_cleanup queries each backend's `prepare_cleanup`,
combines the y/N output, and runs each backend's `cleanup` when
the operator confirms.  Mocks the backends and stdin."""

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
    def test_iterates_every_backend(self):
        docker, docker_plan = _make_backend(empty=False)
        smol, smol_plan = _make_backend(empty=False)
        backends_by_name = {"docker": docker, "smolmachines": smol}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "smolmachines"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))

        docker.prepare_cleanup.assert_called_once()
        smol.prepare_cleanup.assert_called_once()
        docker.cleanup.assert_called_once_with(docker_plan)
        smol.cleanup.assert_called_once_with(smol_plan)

    def test_short_circuits_when_all_empty(self):
        docker, _ = _make_backend(empty=True)
        smol, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "smolmachines": smol}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "smolmachines"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],
        ), patch.object(
            cmd, "_prompt_yes",
        ) as prompt:
            self.assertEqual(0, cmd.cmd_cleanup([]))
        prompt.assert_not_called()
        docker.cleanup.assert_not_called()
        smol.cleanup.assert_not_called()

    def test_abort_at_prompt_runs_nothing(self):
        docker, _ = _make_backend(empty=False)
        smol, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "smolmachines": smol}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "smolmachines"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],
        ), patch.object(
            cmd, "_prompt_yes", return_value=False,
        ):
            self.assertEqual(0, cmd.cmd_cleanup([]))
        docker.cleanup.assert_not_called()
        smol.cleanup.assert_not_called()

    def test_skips_empty_plans_when_others_have_work(self):
        # docker has work, smolmachines doesn't — only docker.cleanup
        # is called.
        docker, docker_plan = _make_backend(empty=False)
        smol, _ = _make_backend(empty=True)
        backends_by_name = {"docker": docker, "smolmachines": smol}

        with patch.object(
            cmd, "known_backend_names",
            return_value=("docker", "smolmachines"),
        ), patch.object(
            cmd, "get_bottle_backend",
            side_effect=lambda name: backends_by_name[name],
        ), patch.object(
            cmd, "_prompt_yes", return_value=True,
        ):
            cmd.cmd_cleanup([])
        docker.cleanup.assert_called_once_with(docker_plan)
        smol.cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
