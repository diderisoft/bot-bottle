"""Unit: backend-neutral workspace planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend import BottleSpec
from bot_bottle.manifest import Manifest
from bot_bottle.workspace import workspace_plan


def _spec(*, copy_cwd: bool, user_cwd: str) -> BottleSpec:
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    return BottleSpec(
        manifest=manifest,
        agent_name="demo",
        copy_cwd=copy_cwd,
        user_cwd=user_cwd,
    )


class TestWorkspacePlan(unittest.TestCase):
    def test_disabled_uses_guest_home_as_workdir(self):
        plan = workspace_plan(
            _spec(copy_cwd=False, user_cwd="/tmp/project"),
            guest_home="/home/node",
        )
        self.assertFalse(plan.enabled)
        self.assertEqual("/home/node", plan.guest_path)
        self.assertEqual("/home/node", plan.workdir)

    def test_enabled_uses_workspace_under_guest_home(self):
        plan = workspace_plan(
            _spec(copy_cwd=True, user_cwd="/tmp/project"),
            guest_home="/guest/home",
        )
        self.assertTrue(plan.enabled)
        self.assertEqual(Path("/tmp/project"), plan.host_path)
        self.assertEqual("/guest/home/workspace", plan.guest_path)
        self.assertEqual("/guest/home/workspace", plan.workdir)

    def test_detects_host_git_dir(self):
        with tempfile.TemporaryDirectory(prefix="bb-workspace.") as tmp:
            Path(tmp, ".git").mkdir()
            plan = workspace_plan(
                _spec(copy_cwd=True, user_cwd=tmp),
                guest_home="/home/node",
            )
        self.assertTrue(plan.has_host_git_dir)


if __name__ == "__main__":
    unittest.main()
