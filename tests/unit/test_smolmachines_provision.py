"""Unit: smolmachines provisioning helpers (PRD 0023 chunks 4a + 4d).

Tests mock `bottle.exec` / `bottle.cp_in` and assert on the
dispatched script shape. The real round-trip lives in the chunk-4
integration smoke."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.agent_provider import (
    AgentProvider,
    AgentProviderRuntime,
    AgentProvisionCommand,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.smolmachines.bottle import SmolmachinesBottle
from bot_bottle.backend.smolmachines.bottle_plan import (
    SmolmachinesBottlePlan,
)
# from bot_bottle.backend.smolmachines.provision import (
#     workspace as _workspace,
# )
from bot_bottle.backend.smolmachines.launch import _bundle_launch_spec
from bot_bottle.backend.util import AGENT_CA_PATH
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream
from bot_bottle.manifest import ManifestGitEntry, ManifestKeyConfig, Manifest
from bot_bottle.supervise import SupervisePlan


class _Provider(AgentProvider):
    """Minimal concrete subclass for testing the default provision_ca/provision_git."""
    @property
    def runtime(self) -> AgentProviderRuntime:
        return AgentProviderRuntime(
            template="test", command="test", image="",
            prompt_mode="append_file", bypass_args=(), resume_args=(),
            remote_control_args=(),
        )
    def provision_plan(self, **kwargs):  # type: ignore[override]
        raise NotImplementedError
    def provision_skills(self, plan, bottle): ...  # type: ignore[override]
    def provision_prompt(self, plan, bottle): ...  # type: ignore[override]
    def provision(self, plan, bottle): ...  # type: ignore[override]
    def provision_supervise_mcp(self, plan, bottle, supervise_url): ...  # type: ignore[override]


_PROVIDER = _Provider()


def _make_bottle(
    name: str = "bot-bottle-demo-abc12",
    exec_result: ExecResult | None = None,
) -> MagicMock:
    bottle = MagicMock(spec=Bottle)
    bottle.name = name
    bottle.exec.return_value = (
        exec_result if exec_result is not None
        else ExecResult(returncode=0, stdout="", stderr="")
    )
    return bottle


def _exec_users(bottle: MagicMock) -> list[str]:  # type: ignore
    """user= kwarg from each bottle.exec call, in order."""
    return [c.kwargs.get("user", "node") for c in bottle.exec.call_args_list]


def _plan(
    *,
    agent_prompt: str = "",
    skills: list[str] | None = None,
    git: list[ManifestGitEntry] = (),  # type: ignore
    git_user: dict | None = None,  # type: ignore
    copy_cwd: bool = False,
    user_cwd: str = "/tmp/x",
    stage_dir: Path | None = None,
    egress_routes: tuple[EgressRoute, ...] = (),
    egress_ca_path: Path = Path(),
    supervise: bool = False,
    bundle_ip: str = "192.168.50.2",
    agent_git_gate_host: str = "127.0.0.1:55555",
    agent_supervise_url: str = "http://127.0.0.1:55556/",
    codex_auth_file: Path | None = None,
    agent_provider_template: str = "claude",
    guest_env: dict[str, str] | None = None,
) -> SmolmachinesBottlePlan:
    bottle_json: dict = {}  # type: ignore
    git_gate_json: dict = {}  # type: ignore
    if git:
        git_gate_json["repos"] = {
            g.Name: {
                "url": g.Upstream,
                "key": {"provider": g.Key.provider or "static", "path": g.Key.path or g.IdentityFile},
            }
            for g in git
        }
    if git_user is not None:
        git_gate_json["user"] = git_user
    if git_gate_json:
        bottle_json["git-gate"] = git_gate_json
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
        manifest=manifest,
        agent_name="demo",
        copy_cwd=copy_cwd,
        user_cwd=user_cwd,
    )
    supervise_plan = None
    if supervise:
        supervise_plan = SupervisePlan(
            slug="demo-abc12",
            queue_dir=Path("/tmp/queue"),
            current_config_dir=Path("/tmp/current-config"),
        )
    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=stage_dir or Path("/tmp/stage"),
        slug="demo-abc12",
        bundle_subnet="192.168.50.0/24",
        bundle_gateway="192.168.50.1",
        bundle_ip=bundle_ip,
        guest_env=dict(guest_env or {}),
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
            routes=egress_routes,
            token_env_map={},
            mitmproxy_ca_cert_only_host_path=egress_ca_path,
        ),
        supervise_plan=supervise_plan,
        agent_git_gate_host=agent_git_gate_host,
        agent_supervise_url=agent_supervise_url,
        agent_provision=_agent_provision(
            agent_provider_template,
            codex_auth_file=codex_auth_file,
            guest_env=dict(guest_env or {}),
        ),
    )


def _agent_provision(
    template: str,
    *,
    codex_auth_file: Path | None = None,
    guest_env: dict[str, str] | None = None,
) -> AgentProvisionPlan:
    if template != "codex":
        return AgentProvisionPlan(
            template=template,
            command=template,
            prompt_mode="append_file",
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-demo-abc12",
            prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
            guest_env=dict(guest_env or {}),
        )
    auth_dir = (guest_env or {}).get("CODEX_HOME", "/home/node/.codex")
    files = [
        AgentProvisionFile(
            Path("/tmp/codex-config.toml"),
            f"{auth_dir}/config.toml",
        ),
    ]
    pre_copy: tuple[AgentProvisionCommand, ...] = ()
    verify: tuple[AgentProvisionCommand, ...] = ()
    if codex_auth_file is not None:
        files.append(AgentProvisionFile(codex_auth_file, f"{auth_dir}/auth.json"))
        pre_copy = (AgentProvisionCommand((
            "find", auth_dir,
            "-maxdepth", "1",
            "-type", "f",
            "(",
            "-name", "*.sqlite",
            "-o", "-name", "*.sqlite-*",
            "-o", "-name", "*.codex-repair-*.bak",
            ")",
            "-delete",
        ), "codex host credentials: could not reset runtime db files"),)
        verify = (AgentProvisionCommand((
            "runuser", "-u", "node", "--",
            "env",
            "HOME=/home/node",
            f"CODEX_HOME={auth_dir}",
            "codex", "login", "status",
        ), "codex host credentials: dummy auth was copied into the guest"),)
    return AgentProvisionPlan(
        template="codex",
        command="codex",
        prompt_mode="read_prompt_file",
        image="bot-bottle-codex:latest",
        dockerfile="",
        guest_home="/home/node",
        instance_name="bot-bottle-demo-abc12",
        prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
        guest_env=dict(guest_env or {}),
        dirs=(AgentProvisionDir(auth_dir),),
        files=tuple(files),
        pre_copy=pre_copy,
        verify=verify,
    )


def _write_self_signed_cert(path: Path) -> None:
    """Drop a real self-signed PEM at `path` so provision_ca's
    fingerprint computation (PEM_cert_to_DER_cert + sha256) has
    actual bytes to chew on. Generated once per test via openssl."""
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", "/dev/null",
         "-out", str(path),
         "-days", "1",
         "-subj", "/CN=test"],
        check=True, capture_output=True,
    )


class TestProvisionCA(unittest.TestCase):
    """provision_ca always uses the egress MITM CA and dispatches
    cp_in + exec in the right order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-ca.")  # pylint: disable=consider-using-with
        self.tmp = Path(self._tmp.name)
        self.egress_ca = self.tmp / "egress-ca.pem"
        _write_self_signed_cert(self.egress_ca)

    def tearDown(self):
        self._tmp.cleanup()

    # provision_ca dies hard if update-ca-certificates' exit
    # is non-zero; supply a stock success return so the bulk of
    # the tests below exercise the happy path.
    _UPDATE_OK = ExecResult(
        returncode=0,
        stdout="Updating certificates in /etc/ssl/certs...\n1 added, 0 removed; done.\n",
        stderr="",
    )

    def test_egress_ca_always_installed(self):
        plan = _plan(egress_ca_path=self.egress_ca)
        bottle = _make_bottle(exec_result=self._UPDATE_OK)
        _PROVIDER.provision_ca(bottle, plan)
        bottle.cp_in.assert_called_once_with(
            str(self.egress_ca),
            AGENT_CA_PATH,
        )
        bottle.exec.assert_called_once()
        script = bottle.exec.call_args.args[0]
        self.assertIn("chmod 644", script)
        self.assertIn("update-ca-certificates", script)
        self.assertEqual("root", bottle.exec.call_args.kwargs.get("user"))

    def test_dies_when_egress_cert_missing(self):
        plan = _plan(egress_ca_path=self.tmp / "does-not-exist.pem")
        bottle = _make_bottle()
        with self.assertRaises(SystemExit):
            _PROVIDER.provision_ca(bottle, plan)


class TestSmolmachinesBottleExec(unittest.TestCase):
    """SmolmachinesBottle.exec retries once on SIGKILL (exit 137)."""

    _SIGKILL = subprocess.CompletedProcess(
        args=[], returncode=137, stdout="", stderr="",
    )
    _SUCCESS = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="done", stderr="",
    )

    def test_retries_on_sigkill(self):
        bottle = SmolmachinesBottle("test-machine")
        with patch(
            "bot_bottle.backend.smolmachines.bottle.subprocess.run",
            side_effect=[self._SIGKILL, self._SUCCESS],
        ) as mock_run, patch(
            "bot_bottle.backend.smolmachines.bottle.time.sleep"
        ) as mock_sleep:
            result = bottle.exec("echo hi")

        self.assertEqual(0, result.returncode)
        self.assertEqual(2, mock_run.call_count)
        mock_sleep.assert_called_once_with(1.0)

    def test_no_retry_on_success(self):
        bottle = SmolmachinesBottle("test-machine")
        with patch(
            "bot_bottle.backend.smolmachines.bottle.subprocess.run",
            return_value=self._SUCCESS,
        ) as mock_run:
            result = bottle.exec("echo hi")

        self.assertEqual(0, result.returncode)
        self.assertEqual(1, mock_run.call_count)

    def test_no_retry_on_other_error(self):
        fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
        bottle = SmolmachinesBottle("test-machine")
        with patch(
            "bot_bottle.backend.smolmachines.bottle.subprocess.run",
            return_value=fail,
        ) as mock_run:
            result = bottle.exec("bad-cmd")

        self.assertEqual(1, result.returncode)
        self.assertEqual(1, mock_run.call_count)


class TestProvisionGit(unittest.TestCase):
    """provision_git writes gitconfig insteadOf rules when configured."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-git.")  # pylint: disable=consider-using-with
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_no_cwd_and_no_git_entries(self):
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, _plan(stage_dir=self.stage))
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_writes_gitconfig_with_ip_port_form_for_smolmachines(self):
        # Smolmachines's TSI-allowlisted guest dials git-gate via
        # smart HTTP at `127.0.0.1:<host port>` — the bundle's
        # git HTTP port is published on host loopback at launch
        # time, and the plan carries the discovered host port.
        plan = _plan(
            git=[ManifestGitEntry(
                Name="bot-bottle",
                Upstream="ssh://git@host/repo.git",
                Key=ManifestKeyConfig(provider="static", path="~/.ssh/id_ed25519"),
                IdentityFile="~/.ssh/id_ed25519",
            )],
            stage_dir=self.stage,
            agent_git_gate_host="127.0.0.1:9418",
        )
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
        # The staged gitconfig path is whatever NamedTemporaryFile
        # picked; we read its contents.
        cp_call = bottle.cp_in.call_args
        staged_path = Path(cp_call.args[0])
        self.assertEqual(self.stage, staged_path.parent)
        content = staged_path.read_text(encoding="utf-8")
        self.assertIn(
            '[url "http://127.0.0.1:9418/bot-bottle.git"]', content,
        )
        self.assertIn(
            "\tinsteadOf = ssh://git@host/repo.git", content,
        )


class TestBundleLaunchSpec(unittest.TestCase):
    def test_git_gate_uses_http_daemon_for_smolmachines(self):
        plan = _plan()
        plan = replace(
            plan,
            git_gate_plan=replace(
                plan.git_gate_plan,
                upstreams=(GitGateUpstream(
                    name="bot-bottle",
                    upstream_url="ssh://git@host/repo.git",
                    upstream_host="host",
                    upstream_port="22",
                    identity_file="/tmp/key",
                    known_host_key="",
                ),),
            ),
        )

        spec = _bundle_launch_spec(plan, "net", "127.0.0.16")

        self.assertEqual(
            "egress,git-gate,git-http",
            spec.daemons_csv,
        )
        self.assertIn(9420, spec.ports_to_publish)
        self.assertNotIn(9418, spec.ports_to_publish)


class TestProvisionGitUser(unittest.TestCase):
    """`provision_git` runs `git config --global` inside the
    guest as the node user. SmolmachinesBottle.exec sets HOME and
    USER automatically for the requested user, so --global lands
    in /home/node/.gitconfig. No-op when the bottle didn't declare
    git_user (issue #86)."""

    def _git_config_calls(self, bottle: MagicMock) -> list[tuple[str, str]]:
        """Filter bottle.exec calls down to git-config invocations,
        return list of (script, user) tuples."""
        out = []
        for c in bottle.exec.call_args_list:
            script = c.args[0] if c.args else ""
            user = c.kwargs.get("user", "node")
            if "git config" in script:
                out.append((script, user))
        return out

    def test_noop_when_no_git_user(self):
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, _plan())
        self.assertEqual([], self._git_config_calls(bottle))

    def test_sets_name_and_email_as_node(self):
        plan = _plan(git_user={
            "name": "Eric Bauerfeld",
            "email": "eric@dideric.is",
        })
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
        calls = self._git_config_calls(bottle)
        self.assertEqual(2, len(calls))
        # Both run as node so SmolmachinesBottle.exec sets HOME=/home/node
        # automatically, ensuring --global writes to /home/node/.gitconfig.
        for script, user in calls:
            self.assertEqual("node", user)
            self.assertIn("git config --global", script)
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Eric Bauerfeld", calls[0][0])
        self.assertIn("user.email", calls[1][0])
        self.assertIn("eric@dideric.is", calls[1][0])

    def test_name_only(self):
        plan = _plan(git_user={"name": "Bot"})
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
        calls = self._git_config_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Bot", calls[0][0])

    def test_email_only(self):
        plan = _plan(git_user={"email": "bot@example.com"})
        bottle = _make_bottle()
        _PROVIDER.provision_git(bottle, plan)
        calls = self._git_config_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.email", calls[0][0])
        self.assertIn("bot@example.com", calls[0][0])


if __name__ == "__main__":
    unittest.main()
