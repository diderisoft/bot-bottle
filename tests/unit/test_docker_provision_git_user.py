"""Unit: docker backend `_provision_git_user` (issue #86).

Mocks `subprocess.run` and asserts the `docker exec -u node …
git config --global …` argv shape. The cwd + git-gate passes
are covered indirectly by the existing integration-shaped tests
in test_smolmachines_provision; this file targets just the new
git_user pass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.backend import BottleSpec
from claude_bottle.backend.docker.bottle_plan import DockerBottlePlan
from claude_bottle.backend.docker.provision import git as _git
from claude_bottle.egress import EgressPlan
from claude_bottle.git_gate import GitGatePlan
from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import PipelockProxyPlan


def _plan(*, git_user: dict | None = None,
          stage_dir: Path | None = None) -> DockerBottlePlan:
    bottle_json: dict = {}
    if git_user is not None:
        bottle_json["git_user"] = git_user
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": bottle_json},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    spec = BottleSpec(
        manifest=manifest, agent_name="demo",
        copy_cwd=False, user_cwd="/tmp/x",
    )
    return DockerBottlePlan(
        spec=spec,
        stage_dir=stage_dir or Path("/tmp/stage"),
        slug="demo-abc12",
        container_name="claude-bottle-demo-abc12",
        container_name_pinned=False,
        image="claude-bottle:latest",
        derived_image="",
        runtime_image="claude-bottle:latest",
        dockerfile_path="",
        env_file=Path("/tmp/agent.env"),
        forwarded_env={},
        prompt_file=Path("/tmp/prompt.txt"),
        proxy_plan=PipelockProxyPlan(
            yaml_path=Path("/tmp/pipelock.yaml"), slug="demo-abc12",
        ),
        git_gate_plan=GitGatePlan(
            slug="demo-abc12",
            entrypoint_script=Path("/tmp/git-gate-entrypoint.sh"),
            hook_script=Path("/tmp/git-gate-hook"),
            access_hook_script=Path("/tmp/git-gate-access-hook"),
            upstreams=(),
        ),
        egress_plan=EgressPlan(
            slug="demo-abc12",
            routes_path=Path("/tmp/routes.yaml"),
            routes=(),
            token_env_map={},
        ),
        supervise_plan=None,
        use_runsc=False,
    )


def _git_config_calls(mock_run) -> list[list[str]]:
    """Filter `subprocess.run` calls down to the ones that run
    `git config --global` inside the bottle, returning each argv."""
    out: list[list[str]] = []
    for call in mock_run.call_args_list:
        argv = call.args[0]
        if (len(argv) >= 5
                and argv[0] == "docker" and argv[1] == "exec"
                and "git" in argv and "config" in argv):
            out.append(list(argv))
    return out


class TestProvisionGitUser(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-git-user.")
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_no_git_user(self):
        with patch.object(_git.subprocess, "run") as run:
            _git._provision_git_user(
                _plan(stage_dir=self.stage), "claude-bottle-demo-abc12",
            )
        self.assertEqual([], _git_config_calls(run))

    def test_sets_name_and_email(self):
        plan = _plan(
            git_user={"name": "Eric Bauerfeld", "email": "eric@dideric.is"},
            stage_dir=self.stage,
        )
        with patch.object(_git.subprocess, "run") as run:
            _git._provision_git_user(plan, "claude-bottle-demo-abc12")
        calls = _git_config_calls(run)
        self.assertEqual(2, len(calls))
        # All `docker exec` invocations run as `-u node` so the
        # --global config lands in /home/node/.gitconfig.
        for argv in calls:
            self.assertEqual(
                ["docker", "exec", "-u", "node", "claude-bottle-demo-abc12",
                 "git", "config", "--global"],
                argv[:8],
            )
        self.assertEqual(["user.name", "Eric Bauerfeld"], calls[0][8:])
        self.assertEqual(["user.email", "eric@dideric.is"], calls[1][8:])

    def test_name_only_sets_only_name(self):
        plan = _plan(git_user={"name": "Bot"}, stage_dir=self.stage)
        with patch.object(_git.subprocess, "run") as run:
            _git._provision_git_user(plan, "claude-bottle-demo-abc12")
        calls = _git_config_calls(run)
        self.assertEqual(1, len(calls))
        self.assertEqual(["user.name", "Bot"], calls[0][8:])

    def test_email_only_sets_only_email(self):
        plan = _plan(
            git_user={"email": "bot@example.com"}, stage_dir=self.stage,
        )
        with patch.object(_git.subprocess, "run") as run:
            _git._provision_git_user(plan, "claude-bottle-demo-abc12")
        calls = _git_config_calls(run)
        self.assertEqual(1, len(calls))
        self.assertEqual(["user.email", "bot@example.com"], calls[0][8:])


if __name__ == "__main__":
    unittest.main()
