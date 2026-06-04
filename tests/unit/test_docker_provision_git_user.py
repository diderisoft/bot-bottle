"""Unit: docker backend `_provision_git_user` (issue #86).

Mocks `bottle.exec` / `bottle.cp_in` and asserts on the script
strings and user parameter. The cwd + git-gate passes are covered
indirectly by the existing integration-shaped tests in
test_smolmachines_provision; this file targets just the git_user
pass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.docker.provision import git as _git
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.workspace import workspace_plan


def _plan(*, git_user: dict | None = None,  # type: ignore
          copy_cwd: bool = False,
          user_cwd: str = "/tmp/x",
          stage_dir: Path | None = None) -> DockerBottlePlan:
    bottle_json: dict = {}  # type: ignore
    if git_user is not None:
        bottle_json["git-gate"] = {"user": git_user}
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": bottle_json},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    spec = BottleSpec(
        manifest=manifest, agent_name="demo",
        copy_cwd=copy_cwd, user_cwd=user_cwd,
    )
    return DockerBottlePlan(
        guest_home="/home/node",
        spec=spec,
        stage_dir=stage_dir or Path("/tmp/stage"),
        slug="demo-abc12",
        container_name="bot-bottle-demo-abc12",
        container_name_pinned=False,
        image="bot-bottle-claude:latest",
        derived_image="",
        runtime_image="bot-bottle-claude:latest",
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
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_env={},
        ),
        workspace_plan=workspace_plan(spec, guest_home="/home/node"),
    )


def _make_bottle(name: str = "bot-bottle-demo-abc12") -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = name
    bottle.exec.return_value = ExecResult(returncode=0, stdout="", stderr="")
    return bottle


def _git_config_exec_calls(bottle: MagicMock) -> list[tuple[str, str]]:
    """Filter bottle.exec calls to git-config invocations.
    Returns list of (script, user) tuples."""
    out = []
    for c in bottle.exec.call_args_list:
        script = c.args[0] if c.args else c.kwargs.get("script", "")
        user = c.kwargs.get("user", c.args[1] if len(c.args) > 1 else "node")
        if "git config" in script:
            out.append((script, user))
    return out


class TestProvisionGitUser(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-git-user.")
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_no_git_user(self):
        bottle = _make_bottle()
        _git._provision_git_user(_plan(stage_dir=self.stage), bottle)
        self.assertEqual([], _git_config_exec_calls(bottle))

    def test_copies_cwd_git_to_workspace_plan_path(self):
        cwd = self.stage / "cwd"
        (cwd / ".git").mkdir(parents=True)
        plan = _plan(copy_cwd=True, user_cwd=str(cwd), stage_dir=self.stage)
        bottle = _make_bottle()
        _git._provision_cwd_git(plan, bottle)

        bottle.cp_in.assert_called_once_with(
            f"{cwd}/.git",
            "/home/node/workspace/.git",
        )
        chown_calls = [
            c for c in bottle.exec.call_args_list
            if "chown" in (c.args[0] if c.args else "")
        ]
        self.assertEqual(1, len(chown_calls))
        self.assertIn("node:node", chown_calls[0].args[0])
        self.assertIn("/home/node/workspace/.git", chown_calls[0].args[0])

    def test_sets_name_and_email(self):
        plan = _plan(
            git_user={"name": "Eric Bauerfeld", "email": "eric@dideric.is"},
            stage_dir=self.stage,
        )
        bottle = _make_bottle()
        _git._provision_git_user(plan, bottle)
        calls = _git_config_exec_calls(bottle)
        self.assertEqual(2, len(calls))
        for script, user in calls:
            self.assertEqual("node", user)
            self.assertIn("git config --global", script)
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Eric Bauerfeld", calls[0][0])
        self.assertIn("user.email", calls[1][0])
        self.assertIn("eric@dideric.is", calls[1][0])

    def test_name_only_sets_only_name(self):
        plan = _plan(git_user={"name": "Bot"}, stage_dir=self.stage)
        bottle = _make_bottle()
        _git._provision_git_user(plan, bottle)
        calls = _git_config_exec_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Bot", calls[0][0])

    def test_email_only_sets_only_email(self):
        plan = _plan(
            git_user={"email": "bot@example.com"}, stage_dir=self.stage,
        )
        bottle = _make_bottle()
        _git._provision_git_user(plan, bottle)
        calls = _git_config_exec_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.email", calls[0][0])
        self.assertIn("bot@example.com", calls[0][0])


if __name__ == "__main__":
    unittest.main()
