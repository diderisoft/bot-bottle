"""Unit: docker provider auth marker provisioning."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.agent_provider import (
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.docker.provision import provider_auth as _provider_auth
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import PipelockProxyPlan


def _plan(
    *,
    codex_auth_file: Path | None = None,
    agent_provider_template: str = "codex",
) -> DockerBottlePlan:
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": {"agent_provider": {"template": "codex"}}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    return DockerBottlePlan(
        spec=BottleSpec(
            manifest=manifest,
            agent_name="demo",
            copy_cwd=False,
            user_cwd="/tmp/x",
        ),
        stage_dir=Path("/tmp/stage"),
        slug="demo-abc12",
        container_name="bot-bottle-demo-abc12",
        container_name_pinned=False,
        image="bot-bottle-codex:latest",
        derived_image="",
        runtime_image="bot-bottle-codex:latest",
        dockerfile_path="",
        env_file=Path("/tmp/agent.env"),
        forwarded_env={},
        prompt_file=Path("/tmp/prompt.txt"),
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
        agent_provision=_agent_provision(
            agent_provider_template, codex_auth_file=codex_auth_file,
        ),
    )


def _agent_provision(
    template: str, *, codex_auth_file: Path | None = None,
) -> AgentProvisionPlan:
    if template != "codex":
        return AgentProvisionPlan(
            template=template,
            command=template,
            prompt_mode="append_file",
            image="",
            dockerfile="",
            guest_env={},
        )
    files = [
        AgentProvisionFile(
            Path("/tmp/codex-config.toml"),
            "/home/node/.codex/config.toml",
        ),
    ]
    if codex_auth_file is not None:
        files.append(AgentProvisionFile(
            codex_auth_file,
            "/home/node/.codex/auth.json",
        ))
    return AgentProvisionPlan(
        template="codex",
        command="codex",
        prompt_mode="read_prompt_file",
        image="bot-bottle-codex:latest",
        dockerfile="",
        guest_env={},
        dirs=(AgentProvisionDir("/home/node/.codex"),),
        files=tuple(files),
    )


class TestProvisionProviderAuth(unittest.TestCase):
    def test_noop_for_non_codex_provider(self):
        with patch.object(_provider_auth.subprocess, "run") as run:
            _provider_auth.provision_provider_auth(
                _plan(agent_provider_template="claude"), "bot-bottle-demo-abc12",
            )
        self.assertEqual(0, run.call_count)

    def test_codex_provider_trusts_launch_dir_without_auth_file(self):
        with patch.object(_provider_auth.subprocess, "run") as run:
            _provider_auth.provision_provider_auth(
                _plan(), "bot-bottle-demo-abc12",
            )
        argvs = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "mkdir", "-p", "/home/node/.codex"],
            argvs,
        )
        trust_config = next(
            a for a in argvs
            if a[:2] == ["docker", "cp"] and a[2] == "/tmp/codex-config.toml"
        )
        self.assertEqual(
            "bot-bottle-demo-abc12:/home/node/.codex/config.toml",
            trust_config[3],
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chown", "node:node", "/home/node/.codex/config.toml"],
            argvs,
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chmod", "600", "/home/node/.codex/config.toml"],
            argvs,
        )

    def test_copies_dummy_auth_json_to_codex_home(self):
        with patch.object(_provider_auth.subprocess, "run") as run:
            _provider_auth.provision_provider_auth(
                _plan(codex_auth_file=Path("/tmp/codex-auth.json")),
                "bot-bottle-demo-abc12",
            )
        argvs = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "mkdir", "-p", "/home/node/.codex"],
            argvs,
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chown", "node:node", "/home/node/.codex"],
            argvs,
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chmod", "700", "/home/node/.codex"],
            argvs,
        )
        self.assertIn(
            ["docker", "cp", "/tmp/codex-auth.json",
             "bot-bottle-demo-abc12:/home/node/.codex/auth.json"],
            argvs,
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chown", "node:node", "/home/node/.codex/auth.json"],
            argvs,
        )
        self.assertIn(
            ["docker", "exec", "-u", "0", "bot-bottle-demo-abc12",
             "chmod", "600", "/home/node/.codex/auth.json"],
            argvs,
        )


if __name__ == "__main__":
    unittest.main()
