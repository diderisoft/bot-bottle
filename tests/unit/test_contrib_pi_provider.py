"""Unit: PiAgentProvider provisioning (PRD 0058, contrib/pi)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.agent_provider import (
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.contrib.pi.agent_provider import PiAgentProvider
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest


_URL = "http://supervise:9100/"


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
        "bottles": {"dev": {"agent_provider": {"template": "pi"}}},
        "agents": {
            "demo": {
                "skills": list(skills or []),
                "prompt": agent_prompt,
                "bottle": "dev",
            },
        },
    })
    spec = BottleSpec(
        manifest=manifest, agent_name="demo",
        copy_cwd=False, user_cwd="/tmp/x",
    )
    return DockerBottlePlan(
        spec=spec,
        stage_dir=Path("/tmp/stage"),
        slug="demo-abc12",
        forwarded_env={},
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
            template="pi", command="pi", prompt_mode="append_system_prompt",
            image="bot-bottle-pi:latest", dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
            guest_env={},
        ),
    )


class TestPiProvisionPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in_and_chowns(self):
        bottle = _make_bottle()
        result = PiAgentProvider().provision_prompt(
            _plan(agent_prompt="hello"), bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", result)
        bottle.cp_in.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "/home/node/.bot-bottle-prompt.txt",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("chown node:node" in s
                and "/home/node/.bot-bottle-prompt.txt" in s
                for s in scripts)
        )

    def test_returns_none_when_agent_has_no_prompt(self):
        bottle = _make_bottle()
        result = PiAgentProvider().provision_prompt(_plan(agent_prompt=""), bottle)
        self.assertIsNone(result)
        bottle.cp_in.assert_called_once()


class TestPiProvisionSkills(unittest.TestCase):
    def test_noop_when_agent_has_no_skills(self):
        bottle = _make_bottle()
        PiAgentProvider().provision_skills(_plan(skills=[]), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_mkdir_plus_cp_per_skill(self):
        bottle = _make_bottle()
        with patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",  # type: ignore
        ), patch(
            "bot_bottle.contrib.pi.agent_provider.os.path.isdir",
            return_value=True,
        ):
            PiAgentProvider().provision_skills(_plan(skills=["search"]), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("mkdir -p" in s and "/home/node/.pi/agent/skills" in s
                for s in scripts)
        )
        bottle.cp_in.assert_called_once()
        self.assertEqual(
            "/home/node/.pi/agent/skills/search/",
            bottle.cp_in.call_args.args[1],
        )


class TestPiProvision(unittest.TestCase):
    def test_creates_dir_and_copies_models_config(self):
        provision = AgentProvisionPlan(
            template="pi", command="pi", prompt_mode="append_system_prompt",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.pi/agent"),),
            files=(AgentProvisionFile(
                Path("/tmp/pi-models.json"),
                "/home/node/.pi/agent/models.json",
            ),),
        )
        bottle = _make_bottle()
        PiAgentProvider().provision(_plan(agent_provision=provision), bottle)
        bottle.cp_in.assert_called_once_with(
            "/tmp/pi-models.json",
            "/home/node/.pi/agent/models.json",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("mkdir -p" in s and "/home/node/.pi/agent" in s for s in scripts)
        )
        self.assertTrue(
            any("chown" in s and "/home/node/.pi/agent/models.json" in s
                for s in scripts)
        )

    def test_dies_when_dir_creation_fails(self):
        provision = AgentProvisionPlan(
            template="pi", command="pi", prompt_mode="append_system_prompt",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.pi/agent"),),
        )
        bottle = _make_bottle(exec_result=ExecResult(1, "", "mkdir: nope\n"))
        with self.assertRaises(SystemExit):
            PiAgentProvider().provision(_plan(agent_provision=provision), bottle)


class TestPiSuperviseMcp(unittest.TestCase):
    def test_noop(self):
        bottle = _make_bottle()
        PiAgentProvider().provision_supervise_mcp(_plan(), bottle, _URL)
        bottle.exec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
