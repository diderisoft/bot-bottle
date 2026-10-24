"""Unit: CodexAgentProvider provisioning (PRD 0050, contrib/codex).

The Codex provider owns its own skills / prompt / provision /
supervise-mcp end-to-end — symmetric with the claude provider but
not sharing a helper module, since codex's apply steps include
the dummy-auth dance and a `codex login status` verify that have
no claude equivalent."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.agent_provider import (
    AgentProvisionCommand,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.contrib.codex.agent_provider import CodexAgentProvider
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex
from bot_bottle.supervisor.plan import SupervisePlan


_URL = "http://supervise:9100/"
_CODEX_DOCKERFILE = (
    Path(__file__).resolve().parents[2] / "bot_bottle/contrib/codex/Dockerfile"
)


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
    identity_token: str = "",
) -> DockerBottlePlan:
    bottle_json: dict = {"agent_provider": {"template": "codex"}}  # type: ignore
    if supervise:
        bottle_json["supervise"] = True
    index = ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle_json},
        "agents": {
            "demo": {
                "skills": list(skills or []),
                "prompt": agent_prompt,
                "bottle": "dev",
            },
        },
    })
    manifest = index.load_for_agent("demo")
    spec = BottleSpec(
        manifest=index, agent_name="demo",
        copy_cwd=False, user_cwd="/tmp/x",
    )
    supervise_plan = None
    if supervise:
        supervise_plan = SupervisePlan(
            slug="demo-abc12",
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
        supervise_plan=supervise_plan,
        use_runsc=False,
        identity_token=identity_token,
        agent_provision=agent_provision or AgentProvisionPlan(
            template="codex", command="codex", prompt_mode="read_prompt_file",
            image="bot-bottle-codex:latest", dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
            guest_env={},
        ),
        settings=Settings(),
        secrets=MappingSecrets({}),
    )


class TestCodexProvisionPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in_and_chowns(self):
        bottle = _make_bottle()
        r = CodexAgentProvider().provision_prompt(
            _plan(agent_prompt="hello"), bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)
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
        r = CodexAgentProvider().provision_prompt(_plan(agent_prompt=""), bottle)
        self.assertIsNone(r)
        bottle.cp_in.assert_called_once()

    def test_returns_path_when_provider_prompt_exists(self):
        bottle = _make_bottle()
        provision = AgentProvisionPlan(
            template="codex", command="codex",
            prompt_mode="read_prompt_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            has_prompt=True,
        )
        r = CodexAgentProvider().provision_prompt(
            _plan(agent_prompt="", agent_provision=provision), bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)

    def test_writes_tui_settings_into_codex_config(self):
        with tempfile.TemporaryDirectory(prefix="bb-codex-ui.") as tmp:
            state_dir = Path(tmp)
            prompt_file = state_dir / "prompt.txt"
            prompt_file.write_text("Existing instructions.\n")
            plan = CodexAgentProvider().provision_plan(
                dockerfile="",
                state_dir=state_dir,
                instance_name="bot-bottle-demo-abc12",
                prompt_file=prompt_file,
                label="research-ui",
                color="cyan",
            )
            config = (state_dir / "codex-config.toml").read_text()
            prompt_text = prompt_file.read_text()
        self.assertTrue(plan.has_prompt)
        self.assertEqual("Existing instructions.\n", prompt_text)
        self.assertIn("[tui]", config)
        self.assertIn('status_line = ["model-with-reasoning"]', config)
        self.assertIn('terminal_title = ["spinner", "project"]', config)
        self.assertIn('theme = "ansi"', config)


class TestCodexProvisionSkills(unittest.TestCase):
    def test_noop_when_agent_has_no_skills(self):
        bottle = _make_bottle()
        CodexAgentProvider().provision_skills(_plan(skills=[]), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_mkdir_plus_cp_per_skill(self):
        bottle = _make_bottle()
        with patch(
            "bot_bottle.backend.util.host_skill_dir",
            side_effect=lambda n: f"/host/skills/{n}",  # type: ignore
        ), patch(
            "bot_bottle.contrib.codex.agent_provider.os.path.isdir",
            return_value=True,
        ):
            CodexAgentProvider().provision_skills(
                _plan(skills=["init-prd"]), bottle,
            )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("mkdir -p" in s and "/home/node/.claude/skills" in s
                for s in scripts)
        )
        bottle.cp_in.assert_called_once()
        self.assertEqual(
            "/home/node/.claude/skills/init-prd/",
            bottle.cp_in.call_args.args[1],
        )


class TestCodexProvision(unittest.TestCase):
    """Codex's declarative provision step: ~/.codex/ dir + config.toml
    + (optional) dummy-auth.json + `codex login status` verify."""

    def test_creates_dir_and_copies_config(self):
        provision = AgentProvisionPlan(
            template="codex", command="codex",
            prompt_mode="read_prompt_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.codex"),),
            files=(AgentProvisionFile(
                Path("/tmp/codex-config.toml"),
                "/home/node/.codex/config.toml",
            ),),
        )
        bottle = _make_bottle()
        CodexAgentProvider().provision(
            _plan(agent_provision=provision), bottle,
        )
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
            template="codex", command="codex",
            prompt_mode="read_prompt_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
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
        CodexAgentProvider().provision(
            _plan(agent_provision=provision), bottle,
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("find" in s and "-delete" in s for s in scripts))
        self.assertTrue(any("runuser" in s and "codex login status" in s for s in scripts))

    def test_forwarded_credentials_verify_sets_codex_path(self):
        with tempfile.TemporaryDirectory(prefix="bb-codex-auth.") as tmp:
            state_dir = Path(tmp)
            prompt_file = state_dir / "prompt.txt"
            prompt_file.write_text("")
            with patch(
                "bot_bottle.contrib.codex.agent_provider.codex_host_access_token",
                return_value="token",
            ), patch(
                "bot_bottle.contrib.codex.agent_provider.write_codex_dummy_auth_file",
            ):
                provision = CodexAgentProvider().provision_plan(
                    dockerfile="",
                    state_dir=state_dir,
                    instance_name="bot-bottle-demo-abc12",
                    prompt_file=prompt_file,
                    forward_host_credentials=True,
                    host_env={},
                )

        verify_argv = provision.verify[0].argv
        self.assertTrue(any(
            item.startswith("PATH=/home/node/.local/bin:")
            for item in verify_argv
        ))
        self.assertIn(
            "/home/node/.codex/packages/standalone/current/bin/codex",
            verify_argv,
        )

    def test_dies_when_dir_creation_fails(self):
        provision = AgentProvisionPlan(
            template="codex", command="codex",
            prompt_mode="read_prompt_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
            dirs=(AgentProvisionDir("/home/node/.codex"),),
        )
        bottle = _make_bottle(exec_result=ExecResult(1, "", "mkdir: nope\n"))
        with self.assertRaises(SystemExit):
            CodexAgentProvider().provision(
                _plan(agent_provision=provision), bottle,
            )


class TestCodexDockerfile(unittest.TestCase):
    def test_installs_procps_for_remote_control_pid_management(self):
        dockerfile = _CODEX_DOCKERFILE.read_text()
        self.assertIn("procps", dockerfile)


# Codex-supported streamable-HTTP MCP config keys (RawMcpServerConfig in
# codex-rs/config/src/mcp_types.rs). config.toml uses deny_unknown_fields,
# so an entry that names anything outside this set is rejected by the CLI.
_CODEX_HTTP_MCP_KEYS = frozenset({
    "url", "http_headers", "env_http_headers", "bearer_token_env_var",
    # shared (transport-agnostic) keys
    "environment_id", "auth", "startup_timeout_sec", "startup_timeout_ms",
    "tool_timeout_sec", "enabled", "required", "supports_parallel_tool_calls",
    "default_tools_approval_mode", "enabled_tools", "disabled_tools",
    "scopes", "oauth", "oauth_resource", "name", "tools",
})


def _append_target(bottle: MagicMock) -> tuple[str, str]:
    """Reconstruct (config_path, appended_toml) from the base64 append
    script the provider ran."""
    import base64 as _b64

    script = bottle.exec.call_args.args[0]
    # printf %s '<b64>' | base64 -d >> '<path>'
    b64 = script.split("printf %s ", 1)[1].split(" |", 1)[0].strip("'")
    path = script.rsplit(">> ", 1)[1].strip().strip("'")
    return path, _b64.b64decode(b64).decode()


class TestCodexSuperviseMcp(unittest.TestCase):
    def test_noop_when_supervise_disabled(self):
        bottle = _make_bottle()
        CodexAgentProvider().provision_supervise_mcp(
            _plan(supervise=False), bottle, _URL,
        )
        bottle.exec.assert_not_called()

    def test_appends_streamable_http_entry_as_node(self):
        import tomllib

        bottle = _make_bottle()
        plan = _plan(supervise=True, identity_token="tok-abc123")
        CodexAgentProvider().provision_supervise_mcp(plan, bottle, _URL)
        bottle.exec.assert_called_once()
        self.assertEqual("node", bottle.exec.call_args.kwargs.get("user"))

        config_path, appended = _append_target(bottle)
        self.assertEqual("/home/node/.codex/config.toml", config_path)
        # The appended block must be valid TOML and parse to a streamable
        # HTTP server carrying the identity token as a static http header.
        parsed = tomllib.loads(appended)
        server = parsed["mcp_servers"]["supervise"]
        self.assertEqual(_URL, server["url"])
        self.assertEqual(
            "tok-abc123", server["http_headers"]["x-bot-bottle-identity"],
        )
        # Never emit an unsupported key (config.toml is deny_unknown_fields);
        # in particular there is no `--header` / `header` surface.
        self.assertTrue(
            set(server).issubset(_CODEX_HTTP_MCP_KEYS),
            f"unsupported codex mcp keys: {set(server) - _CODEX_HTTP_MCP_KEYS}",
        )
        self.assertNotIn("mcp add", bottle.exec.call_args.args[0])

    def test_appends_to_custom_codex_home(self):
        bottle = _make_bottle()
        provision = AgentProvisionPlan(
            template="codex", command="codex", prompt_mode="read_prompt_file",
            image="", dockerfile="", guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={"CODEX_HOME": "/home/node/alt-codex"},
        )
        plan = _plan(
            supervise=True, agent_provision=provision, identity_token="tok",
        )
        CodexAgentProvider().provision_supervise_mcp(plan, bottle, _URL)
        config_path, _ = _append_target(bottle)
        self.assertEqual("/home/node/alt-codex/config.toml", config_path)

    def test_omits_http_headers_when_no_token(self):
        import tomllib

        bottle = _make_bottle()
        CodexAgentProvider().provision_supervise_mcp(
            _plan(supervise=True), bottle, _URL,
        )
        _, appended = _append_target(bottle)
        server = tomllib.loads(appended)["mcp_servers"]["supervise"]
        self.assertNotIn("http_headers", server)

    def test_registration_failure_is_fatal(self):
        bottle = _make_bottle(
            exec_result=ExecResult(returncode=1, stdout="", stderr="boom"),
        )
        with self.assertRaises(SystemExit):
            CodexAgentProvider().provision_supervise_mcp(
                _plan(supervise=True), bottle, _URL,
            )


class TestCodexHeadlessPrompt(unittest.TestCase):
    def test_returns_prompt_as_positional_arg(self):
        self.assertEqual(["Do the task"], CodexAgentProvider().headless_prompt("Do the task"))

    def test_preserves_prompt_text_verbatim(self):
        text = "Fix issue #42: the widget breaks on empty input"
        self.assertEqual([text], CodexAgentProvider().headless_prompt(text))


if __name__ == "__main__":
    unittest.main()
