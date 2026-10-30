"""Unit: PiAgentProvider provisioning (PRD 0058, contrib/pi)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.contract.identity import IDENTITY_HEADER
from bot_bottle.secret_source import MappingSecrets
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
from bot_bottle.manifest import ManifestIndex


_URL = "http://supervise:9100/"
_PI_DOCKERFILE = Path(__file__).resolve().parents[2] / "bot_bottle/contrib/pi/Dockerfile"
_PI_PACKAGE_JSON = _PI_DOCKERFILE.with_name("package.json")


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
    index = ManifestIndex.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {
            "demo": {
                "skills": list(skills or []),
                "prompt": agent_prompt,
                "bottle": "dev",
                "agent_provider": {"harness": "pi"},
            },
        },
    })
    manifest = index.load_for_agent("demo")
    spec = BottleSpec(
        manifest=index, agent_name="demo",
        copy_cwd=False, user_cwd="/tmp/x",
    )
    return DockerBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=Path("/tmp/stage"),
        slug="demo-abc12",
        forwarded_env={},
        git_gate_plan=GitGatePlan(
            slug="demo-abc12",
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
            harness="pi", command="pi", prompt_mode="append_system_prompt",
            image="bot-bottle-pi:latest", dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
            guest_env={},
        ),
        settings=Settings(),
        secrets=MappingSecrets({}),
    )


class TestPiProvisionPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in_and_chowns(self):
        bottle = _make_bottle()
        result = PiAgentProvider().provision_prompt(
            _plan(agent_prompt="hello"), bottle,
        )
        self.assertIsNone(result)
        bottle.cp_in.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "/home/node/.bot-bottle-prompt.txt",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("chown node:node" in s
                and "/home/node/.bot-bottle-prompt.txt" in s
                and "/home/node/.pi/agent/APPEND_SYSTEM.md" in s
                for s in scripts)
        )
        self.assertTrue(
            any("cp /home/node/.bot-bottle-prompt.txt" in s
                and "/home/node/.pi/agent/APPEND_SYSTEM.md" in s
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
            harness="pi", command="pi", prompt_mode="append_system_prompt",
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
            any("/home/node/.pi/context-mode/sessions" in s
                and "/tmp/pi-subagents-uid-1000" in s
                and "chown node:node /home/node" in s
                and "chown -R node:node /home/node/.pi /tmp" in s
                and "chmod 755 /home/node" in s
                for s in scripts)
        )
        self.assertTrue(
            any("chown" in s and "/home/node/.pi/agent/models.json" in s
                for s in scripts)
        )
        # pi keeps its own input editor: nothing replaces it.
        self.assertFalse(any("remote-view" in s for s in scripts))

    def test_dies_when_dir_creation_fails(self):
        provision = AgentProvisionPlan(
            harness="pi", command="pi", prompt_mode="append_system_prompt",
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
    def test_noop_without_supervise(self):
        bottle = _make_bottle()
        PiAgentProvider().provision_supervise_mcp(_plan(), bottle, _URL)
        bottle.exec.assert_not_called()

    def test_installs_the_extension_and_its_config(self):
        with tempfile.TemporaryDirectory() as home:
            plan = SimpleNamespace(supervise_plan=object(), identity_token="tok-1",
                                   guest_home=home)
            bottle = _make_bottle()

            def run(script: str, user: str) -> ExecResult:
                del user
                return ExecResult(subprocess.run(["sh", "-c", script], check=False).returncode,
                                  "", "")

            bottle.exec.side_effect = run
            PiAgentProvider().provision_supervise_mcp(plan, bottle, _URL)  # type: ignore[arg-type]
            agent = Path(home, ".pi/agent")
            self.assertEqual(
                {"url": _URL, "header": IDENTITY_HEADER, "token": "tok-1"},
                json.loads((agent / "bot-bottle-supervise.json").read_text()))
            self.assertEqual(0o600, (agent / "bot-bottle-supervise.json").stat().st_mode & 0o777)
            self.assertEqual(
                _PI_DOCKERFILE.with_name("supervise.ts").read_text(),
                (agent / "extensions/bot-bottle-supervise.ts").read_text())
            self.assertEqual("node", bottle.exec.call_args.kwargs["user"])

    def test_dies_when_it_cannot_install(self):
        plan = SimpleNamespace(supervise_plan=object(), identity_token="", guest_home="/x")
        bottle = _make_bottle(exec_result=ExecResult(1, "", "denied\n"))
        with self.assertRaises(SystemExit):
            PiAgentProvider().provision_supervise_mcp(plan, bottle, _URL)  # type: ignore[arg-type]


class TestPiDockerfile(unittest.TestCase):
    def test_installs_exact_pi_cwd_from_the_committed_lock(self):
        dockerfile = _PI_DOCKERFILE.read_text()
        package = json.loads(_PI_PACKAGE_JSON.read_text())
        version = package["dependencies"]["@harms-haus/pi-cwd"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertIn("npm ci --omit=dev", dockerfile)
        self.assertNotIn("pi install", dockerfile)

    def test_prepares_pi_extension_state_dirs_and_tmp_for_node(self):
        dockerfile = _PI_DOCKERFILE.read_text()
        self.assertIn("/home/node/.pi/context-mode/sessions", dockerfile)
        self.assertIn("/tmp/pi-subagents-uid-1000", dockerfile)
        self.assertIn("chown -R node:node /home/node/.pi /tmp", dockerfile)
        self.assertIn("chmod -R u+rwX /tmp", dockerfile)
        self.assertIn("chown root:root /tmp /var/tmp", dockerfile)
        self.assertIn("chmod 1777 /tmp /var/tmp", dockerfile)


class TestPiHeadlessPrompt(unittest.TestCase):
    def test_returns_p_flag_and_prompt(self):
        self.assertEqual(["-p", "Do the task"], PiAgentProvider().headless_prompt("Do the task"))

    def test_preserves_prompt_text_verbatim(self):
        text = "Fix issue #42: the widget breaks on empty input"
        self.assertEqual(["-p", text], PiAgentProvider().headless_prompt(text))


if __name__ == "__main__":
    unittest.main()
