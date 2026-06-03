"""Unit: shared provision-apply helpers (PRD 0050).

Covers `bot_bottle._provision_apply.apply_skills` /
`apply_prompt` / `apply_provision` — the backend-agnostic helpers
that AgentProvider's default `provision_skills` / `provision_prompt`
/ `provision` dispatch through. The same suite covered the
docker / smolmachines `provision/{skills,prompt,provider_auth}.py`
modules before they were deleted."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle import _provision_apply
from bot_bottle._provision_apply import (
    PROMPT_PATH,
    SKILLS_DIR,
    apply_prompt,
    apply_provision,
    apply_skills,
)
from bot_bottle.agent_provider import (
    AgentProvisionCommand,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.workspace import workspace_plan


def _make_bottle(exec_result: ExecResult | None = None) -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = "bot-bottle-demo-abc12"
    bottle.exec.return_value = (
        exec_result if exec_result is not None
        else ExecResult(returncode=0, stdout="", stderr="")
    )
    return bottle


def _exec_scripts(bottle: MagicMock) -> list[str]:
    return [c.args[0] for c in bottle.exec.call_args_list]


def _plan(
    *,
    agent_prompt: str = "",
    skills: list[str] | None = None,
    agent_provision: AgentProvisionPlan | None = None,
) -> DockerBottlePlan:
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {
            "demo": {
                "skills": list(skills or []),
                "prompt": agent_prompt,
                "bottle": "dev",
            },
        },
    })
    spec = BottleSpec(
        manifest=manifest,
        agent_name="demo",
        copy_cwd=False,
        user_cwd="/tmp/x",
    )
    return DockerBottlePlan(
        spec=spec,
        stage_dir=Path("/tmp/stage"),
        slug="demo-abc12",
        container_name="bot-bottle-demo-abc12",
        container_name_pinned=False,
        image="bot-bottle-claude:latest",
        derived_image="",
        runtime_image="bot-bottle-claude:latest",
        dockerfile_path="",
        env_file=Path("/tmp/agent.env"),
        forwarded_env={},
        prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
        proxy_plan=PipelockProxyPlan(
            yaml_path=Path("/tmp/pipelock.yaml"),
            slug="demo-abc12",
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
        agent_provision=agent_provision or AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="",
            dockerfile="",
            guest_env={},
        ),
        workspace_plan=workspace_plan(spec, guest_home="/home/node"),
    )


class TestApplyPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in(self):
        bottle = _make_bottle()
        apply_prompt(_plan(), bottle)
        bottle.cp_in.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            PROMPT_PATH,
        )

    def test_returns_path_when_agent_has_prompt(self):
        bottle = _make_bottle()
        r = apply_prompt(_plan(agent_prompt="You are a helpful assistant."), bottle)
        self.assertEqual(PROMPT_PATH, r)

    def test_returns_none_when_agent_has_no_prompt(self):
        bottle = _make_bottle()
        r = apply_prompt(_plan(agent_prompt=""), bottle)
        self.assertIsNone(r)
        bottle.cp_in.assert_called_once()

    def test_chowns_to_node_after_copy(self):
        bottle = _make_bottle()
        apply_prompt(_plan(), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("chown node:node" in s and PROMPT_PATH in s for s in scripts))
        self.assertTrue(any("chmod 600" in s and PROMPT_PATH in s for s in scripts))


class TestApplySkills(unittest.TestCase):
    def test_noop_when_agent_has_no_skills(self):
        bottle = _make_bottle()
        apply_skills(_plan(skills=[]), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_mkdir_plus_cp_per_skill(self):
        bottle = _make_bottle()
        with patch.object(
            _provision_apply, "host_skill_dir", create=True,
            side_effect=lambda n: f"/host/skills/{n}",
        ) if False else patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",
        ), patch("bot_bottle._provision_apply.os.path.isdir", return_value=True):
            apply_skills(_plan(skills=["init-prd", "verify"]), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("mkdir -p" in s and SKILLS_DIR in s for s in scripts))
        cp_targets = {c.args[1] for c in bottle.cp_in.call_args_list}
        self.assertEqual({
            f"{SKILLS_DIR}/init-prd/",
            f"{SKILLS_DIR}/verify/",
        }, cp_targets)
        self.assertEqual(
            2,
            sum(1 for s in scripts if "chown -R node:node" in s),
        )

    def test_missing_skill_dies(self):
        bottle = _make_bottle()
        with patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",
        ), patch("bot_bottle._provision_apply.os.path.isdir", return_value=False):
            with self.assertRaises(SystemExit):
                apply_skills(_plan(skills=["init-prd"]), bottle)


class TestApplyProvision(unittest.TestCase):
    """The `dirs` / `pre_copy` / `files` / `verify` apply loop that
    used to live in `provision_provider_auth`."""

    def test_noop_on_empty_provision_plan(self):
        bottle = _make_bottle()
        apply_provision(_plan(), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_codex_provision_creates_dir_and_copies_config(self):
        provision = AgentProvisionPlan(
            template="codex",
            command="codex",
            prompt_mode="read_prompt_file",
            image="bot-bottle-codex:latest",
            dockerfile="",
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.codex"),),
            files=(AgentProvisionFile(
                Path("/tmp/codex-config.toml"),
                "/home/node/.codex/config.toml",
            ),),
        )
        bottle = _make_bottle()
        apply_provision(_plan(agent_provision=provision), bottle)
        bottle.cp_in.assert_called_once_with(
            "/tmp/codex-config.toml",
            "/home/node/.codex/config.toml",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("mkdir -p" in s and "/home/node/.codex" in s for s in scripts))
        self.assertTrue(any("chown" in s and "/home/node/.codex/config.toml" in s for s in scripts))
        self.assertTrue(any("chmod" in s and "/home/node/.codex/config.toml" in s for s in scripts))

    def test_runs_pre_copy_then_verify(self):
        provision = AgentProvisionPlan(
            template="codex",
            command="codex",
            prompt_mode="read_prompt_file",
            image="",
            dockerfile="",
            guest_env={},
            pre_copy=(AgentProvisionCommand(
                ("find", "/home/node/.codex", "-name", "*.sqlite", "-delete"),
                "could not reset runtime db files",
            ),),
            verify=(AgentProvisionCommand(
                ("runuser", "-u", "node", "--", "codex", "login", "status"),
                "codex rejected the dummy auth",
            ),),
        )
        bottle = _make_bottle()
        apply_provision(_plan(agent_provision=provision), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("find" in s and "-delete" in s for s in scripts))
        self.assertTrue(any("runuser" in s and "codex login status" in s for s in scripts))

    def test_dies_when_dir_creation_fails(self):
        provision = AgentProvisionPlan(
            template="codex",
            command="codex",
            prompt_mode="read_prompt_file",
            image="",
            dockerfile="",
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.codex"),),
        )
        bottle = _make_bottle(exec_result=ExecResult(1, "", "mkdir: nope\n"))
        with self.assertRaises(SystemExit):
            apply_provision(_plan(agent_provision=provision), bottle)


if __name__ == "__main__":
    unittest.main()
