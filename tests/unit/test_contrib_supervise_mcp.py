"""Unit: contrib supervise MCP registration (PRD 0050).

Each provider plugin's `provision_supervise_mcp` runs the
provider's own CLI (`claude mcp add` / `codex mcp add`) inside the
agent guest to register the per-bottle supervise sidecar in the
provider's user config. The previous claude-only `provision_supervise`
modules under backend/{docker,smolmachines}/provision/supervise.py
covered this behavior pre-PRD-0050."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.contrib.claude.agent_provider import ClaudeAgentProvider
from bot_bottle.contrib.codex.agent_provider import CodexAgentProvider
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.supervise import SupervisePlan
from bot_bottle.workspace import workspace_plan


def _make_bottle(exec_result: ExecResult | None = None) -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = "bot-bottle-demo-abc12"
    bottle.exec.return_value = (
        exec_result if exec_result is not None
        else ExecResult(returncode=0, stdout="", stderr="")
    )
    return bottle


def _plan(*, supervise: bool, template: str = "claude") -> DockerBottlePlan:
    manifest = Manifest.from_json_obj({
        "bottles": {
            "dev": {"agent_provider": {"template": template},
                    **({"supervise": True} if supervise else {})},
        },
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
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
        supervise_plan=supervise_plan,
        use_runsc=False,
        agent_provision=AgentProvisionPlan(
            template=template, command=template,
            prompt_mode="append_file" if template == "claude" else "read_prompt_file",
            image="", dockerfile="", guest_env={},
        ),
        workspace_plan=workspace_plan(spec, guest_home="/home/node"),
    )


_URL = "http://supervise:9100/"


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


class TestCodexSuperviseMcp(unittest.TestCase):
    def test_noop_when_supervise_disabled(self):
        bottle = _make_bottle()
        CodexAgentProvider().provision_supervise_mcp(
            _plan(supervise=False, template="codex"), bottle, _URL,
        )
        bottle.exec.assert_not_called()

    def test_runs_codex_mcp_add_as_node(self):
        bottle = _make_bottle()
        CodexAgentProvider().provision_supervise_mcp(
            _plan(supervise=True, template="codex"), bottle, _URL,
        )
        bottle.exec.assert_called_once()
        script = bottle.exec.call_args.args[0]
        self.assertEqual("node", bottle.exec.call_args.kwargs.get("user"))
        self.assertIn("codex mcp add", script)
        self.assertIn("--transport http", script)
        self.assertIn("supervise", script)
        self.assertIn(_URL, script)

    def test_logs_warning_on_failure_but_does_not_raise(self):
        bottle = _make_bottle(
            exec_result=ExecResult(returncode=1, stdout="", stderr="boom"),
        )
        CodexAgentProvider().provision_supervise_mcp(
            _plan(supervise=True, template="codex"), bottle, _URL,
        )


if __name__ == "__main__":
    unittest.main()
