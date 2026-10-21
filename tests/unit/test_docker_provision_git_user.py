"""Unit: AgentProvider.provision_git — git-user and cwd .git copy (issue #86).

Mocks bottle.exec / bottle.cp_in and asserts on the dispatched script
shape. provision_git is now a method on AgentProvider (default impl);
the internal passes (_provision_cwd_git, _provision_git_gate_config,
_provision_git_user) are no longer exposed as separate helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.agent_provider import (
    AgentProvider,
    AgentProvisionPlan,
    AgentProviderRuntime,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex


class _Provider(AgentProvider):
    """Minimal concrete subclass for testing the default provision_git."""
    @property
    def runtime(self) -> AgentProviderRuntime:
        return AgentProviderRuntime(
            template="test", command="test", image="",
            prompt_mode="append_file", bypass_args=(), resume_args=(),
        )
    def provision_plan(self, **kwargs):  # type: ignore[override]
        raise NotImplementedError
    def provision_skills(self, plan, bottle): ...  # type: ignore[override]
    def provision_prompt(self, plan, bottle): ...  # type: ignore[override]
    def provision(self, plan, bottle): ...  # type: ignore[override]
    def provision_supervise_mcp(self, plan, bottle, supervise_url): ...  # type: ignore[override]
    def headless_prompt(self, prompt): return []  # type: ignore[override]


_PROVIDER = _Provider()


def _plan(*, git_user: dict | None = None,  # type: ignore
          git_repos: dict | None = None,  # type: ignore
          copy_cwd: bool = False,
          user_cwd: str = "/tmp/x",
          stage_dir: Path | None = None) -> DockerBottlePlan:
    bottle_json: dict = {}  # type: ignore
    if git_user is not None:
        bottle_json["git-gate"] = {"user": git_user}
    if git_repos is not None:
        bottle_json.setdefault("git-gate", {})["repos"] = git_repos
    index = ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle_json},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })
    manifest = index.load_for_agent("demo")
    spec = BottleSpec(
        manifest=index, agent_name="demo",
        copy_cwd=copy_cwd, user_cwd=user_cwd,
    )
    return DockerBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=stage_dir or Path("/tmp/stage"),
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
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/prompt.txt"),
            guest_env={},
        ),
        settings=Settings(),
        secrets=MappingSecrets({}),
    )


def _make_bottle(name: str = "bot-bottle-demo-abc12") -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = name
    bottle.exec.return_value = ExecResult(returncode=0, stdout="", stderr="")
    return bottle


def _git_config_exec_calls(bottle: MagicMock) -> list[tuple[str, str]]:
    out = []
    for c in bottle.exec.call_args_list:
        script = c.args[0] if c.args else c.kwargs.get("script", "")
        user = c.kwargs.get("user", c.args[1] if len(c.args) > 1 else "node")
        if "git config" in script:
            out.append((script, user))
    return out


class TestProvisionGitUser(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-git-user.")  # pylint: disable=consider-using-with
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_no_git_user(self):
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, _plan(stage_dir=self.stage))
        self.assertEqual([], _git_config_exec_calls(bottle))

    def test_repairs_git_xdg_directory_for_runtime_user(self):
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, _plan(stage_dir=self.stage))

        script, user = next(
            (call.args[0], call.kwargs.get("user", "node"))
            for call in bottle.exec.call_args_list
            if "/home/node/.config/git" in call.args[0]
        )
        self.assertEqual("root", user)
        self.assertIn("chown node:node /home/node", script)
        self.assertIn("chmod 755 /home/node", script)
        self.assertIn("mkdir -p /home/node/.config/git", script)
        self.assertIn("chown -R node:node /home/node/.config", script)
        self.assertIn("chmod -R u+rwX,go+rX /home/node/.config", script)

    def test_fails_closed_when_home_permissions_cannot_be_repaired(self):
        bottle = _make_bottle()
        bottle.exec.return_value = ExecResult(1, "", "read-only filesystem")

        with self.assertRaises(SystemExit):
            _PROVIDER.provision_git(bottle, _plan(stage_dir=self.stage))

    def _git_plan(self) -> DockerBottlePlan:
        return _plan(
            git_repos={
                "repo": {
                    "url": "ssh://git@example.com/repo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                    "host_key": "ssh-ed25519 AAAA",
                },
            },
            stage_dir=self.stage,
        )

    def test_fails_closed_when_gitconfig_permissions_cannot_be_set(self):
        bottle = _make_bottle()
        bottle.exec.side_effect = [
            ExecResult(0, "", ""),
            ExecResult(1, "", "chown failed"),
        ]

        with self.assertRaises(SystemExit):
            _PROVIDER.provision_git(bottle, self._git_plan())

    def test_fails_closed_when_runtime_user_cannot_read_gitconfig(self):
        bottle = _make_bottle()
        bottle.exec.side_effect = [
            ExecResult(0, "", ""),
            ExecResult(0, "", ""),
            ExecResult(1, "", "permission denied"),
        ]

        with self.assertRaises(SystemExit):
            _PROVIDER.provision_git(bottle, self._git_plan())

    def test_sets_name_and_email(self):
        plan = _plan(
            git_user={"name": "Eric Bauerfeld", "email": "eric@dideric.is"},
            stage_dir=self.stage,
        )
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
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
        _PROVIDER.provision_git(bottle, plan)
        calls = _git_config_exec_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Bot", calls[0][0])

    def test_email_only_sets_only_email(self):
        plan = _plan(
            git_user={"email": "bot@example.com"}, stage_dir=self.stage,
        )
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
        calls = _git_config_exec_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.email", calls[0][0])
        self.assertIn("bot@example.com", calls[0][0])


if __name__ == "__main__":
    unittest.main()
