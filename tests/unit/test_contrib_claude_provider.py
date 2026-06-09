"""Unit: ClaudeAgentProvider provisioning (PRD 0050, contrib/claude).

Each provider owns its own in-guest provisioning end-to-end —
skills copy, prompt copy, declarative dirs/files/pre_copy/verify
apply, and supervise MCP registration. The Claude / Codex paths
intentionally don't share a helper module: harness changes on
either side are expected to diverge the implementations."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.agent_provider import (
    AgentProvisionCommand,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.contrib.claude.agent_provider import ClaudeAgentProvider
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
from bot_bottle.supervise import SupervisePlan


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
    supervise: bool = False,
) -> DockerBottlePlan:
    bottle_json: dict = {"agent_provider": {"template": "claude"}}  # type: ignore
    if supervise:
        bottle_json["supervise"] = True
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": bottle_json},
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
    supervise_plan = None
    if supervise:
        supervise_plan = SupervisePlan(
            slug="demo-abc12",
            queue_dir=Path("/tmp/queue"),
            current_config_dir=Path("/tmp/current-config"),
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
        supervise_plan=supervise_plan,
        use_runsc=False,
        agent_provision=agent_provision or AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="bot-bottle-claude:latest", dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
            guest_env={},
        ),
    )


class TestClaudeProvisionPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision_prompt(_plan(), bottle)
        bottle.cp_in.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "/home/node/.bot-bottle-prompt.txt",
        )

    def test_returns_path_when_agent_has_prompt(self):
        bottle = _make_bottle()
        r = ClaudeAgentProvider().provision_prompt(
            _plan(agent_prompt="You are helpful."), bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)

    def test_returns_none_when_agent_has_no_prompt(self):
        bottle = _make_bottle()
        r = ClaudeAgentProvider().provision_prompt(_plan(agent_prompt=""), bottle)
        self.assertIsNone(r)
        bottle.cp_in.assert_called_once()

    def test_returns_path_when_provider_prompt_has_identity(self):
        bottle = _make_bottle()
        provision = AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            has_prompt=True,
        )
        r = ClaudeAgentProvider().provision_prompt(
            _plan(agent_prompt="", agent_provision=provision), bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)

    def test_chowns_to_node_after_copy(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision_prompt(_plan(), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("chown node:node" in s
                and "/home/node/.bot-bottle-prompt.txt" in s
                for s in scripts)
        )
        self.assertTrue(
            any("chmod 600" in s
                and "/home/node/.bot-bottle-prompt.txt" in s
                for s in scripts)
        )


class TestClaudeProvisionSkills(unittest.TestCase):
    def test_noop_when_agent_has_no_skills(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision_skills(_plan(skills=[]), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_mkdir_plus_cp_per_skill(self):
        bottle = _make_bottle()
        with patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",  # type: ignore
        ), patch(
            "bot_bottle.contrib.claude.agent_provider.os.path.isdir",
            return_value=True,
        ):
            ClaudeAgentProvider().provision_skills(
                _plan(skills=["init-prd", "verify"]), bottle,
            )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("mkdir -p" in s and "/home/node/.claude/skills" in s
                for s in scripts)
        )
        cp_targets = {c.args[1] for c in bottle.cp_in.call_args_list}
        self.assertEqual({
            "/home/node/.claude/skills/init-prd/",
            "/home/node/.claude/skills/verify/",
        }, cp_targets)
        self.assertEqual(
            2, sum(1 for s in scripts if "chown -R node:node" in s),
        )

    def test_missing_skill_dies(self):
        bottle = _make_bottle()
        with patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",  # type: ignore
        ), patch(
            "bot_bottle.contrib.claude.agent_provider.os.path.isdir",
            return_value=False,
        ):
            with self.assertRaises(SystemExit):
                ClaudeAgentProvider().provision_skills(
                    _plan(skills=["init-prd"]), bottle,
                )


class TestClaudeProvision(unittest.TestCase):
    """The declarative dirs/files/pre_copy/verify apply loop for
    the claude.json trust marker."""

    def test_noop_on_empty_provision_plan(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision(_plan(), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_copies_files_and_chowns(self):
        provision = AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            files=(AgentProvisionFile(
                Path("/tmp/claude.json"), "/home/node/.claude.json",
            ),),
        )
        bottle = _make_bottle()
        ClaudeAgentProvider().provision(
            _plan(agent_provision=provision), bottle,
        )
        bottle.cp_in.assert_called_once_with(
            "/tmp/claude.json", "/home/node/.claude.json",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("chown" in s and "/home/node/.claude.json" in s for s in scripts)
        )
        self.assertTrue(
            any("chmod" in s and "/home/node/.claude.json" in s for s in scripts)
        )

    def test_dies_when_file_chown_fails(self):
        provision = AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            files=(AgentProvisionFile(
                Path("/tmp/claude.json"), "/home/node/.claude.json",
            ),),
        )
        bottle = _make_bottle(
            exec_result=ExecResult(1, "", "chown: no such file\n"),
        )
        with self.assertRaises(SystemExit):
            ClaudeAgentProvider().provision(
                _plan(agent_provision=provision), bottle,
            )

    def test_runs_verify_commands(self):
        provision = AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            verify=(AgentProvisionCommand(
                ("/usr/bin/true",), "verify failed",
            ),),
        )
        bottle = _make_bottle()
        ClaudeAgentProvider().provision(
            _plan(agent_provision=provision), bottle,
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("/usr/bin/true" in s for s in scripts))


class TestClaudeSuperviseMcp(unittest.TestCase):
    def test_noop_when_supervise_disabled(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision_supervise_mcp(
            _plan(supervise=False), bottle, _URL,
        )
        bottle.exec.assert_not_called()

    def test_runs_claude_mcp_add_as_node(self):
        bottle = _make_bottle()
        ClaudeAgentProvider().provision_supervise_mcp(
            _plan(supervise=True), bottle, _URL,
        )
        bottle.exec.assert_called_once()
        script = bottle.exec.call_args.args[0]
        self.assertEqual("node", bottle.exec.call_args.kwargs.get("user"))
        self.assertIn("claude mcp add", script)
        self.assertIn("--scope user", script)
        self.assertIn("--transport http", script)
        self.assertIn("supervise", script)
        self.assertIn(_URL, script)

    def test_logs_warning_on_failure_but_does_not_raise(self):
        bottle = _make_bottle(
            exec_result=ExecResult(returncode=1, stdout="", stderr="boom"),
        )
        ClaudeAgentProvider().provision_supervise_mcp(
            _plan(supervise=True), bottle, _URL,
        )


if __name__ == "__main__":
    unittest.main()
