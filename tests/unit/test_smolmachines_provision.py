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
    AgentProvisionCommand,
    AgentProvisionDir,
    AgentProvisionFile,
    AgentProvisionPlan,
)
from bot_bottle.backend import Bottle, BottleSpec, ExecResult
from bot_bottle.backend.smolmachines.bottle_plan import (
    SmolmachinesBottlePlan,
)
from bot_bottle.backend.smolmachines.provision import (
    ca as _ca,
    git as _git,
    prompt as _prompt,
    provider_auth as _provider_auth,
    skills as _skills,
    supervise as _supervise,
    workspace as _workspace,
)
from bot_bottle.backend.smolmachines.launch import _bundle_launch_spec
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream
from bot_bottle.manifest import GitEntry, Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.supervise import SupervisePlan
from bot_bottle.workspace import workspace_plan


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


def _exec_scripts(bottle: MagicMock) -> list[str]:
    """All script strings passed to bottle.exec, in call order."""
    return [c.args[0] for c in bottle.exec.call_args_list]


def _exec_users(bottle: MagicMock) -> list[str]:
    """user= kwarg from each bottle.exec call, in order."""
    return [c.kwargs.get("user", "node") for c in bottle.exec.call_args_list]


def _plan(
    *,
    agent_prompt: str = "",
    skills: list[str] | None = None,
    git: list[GitEntry] = (),
    git_user: dict | None = None,
    copy_cwd: bool = False,
    user_cwd: str = "/tmp/x",
    stage_dir: Path | None = None,
    egress_routes: tuple[EgressRoute, ...] = (),
    egress_ca_path: Path = Path(),
    pipelock_ca_path: Path = Path(),
    supervise: bool = False,
    bundle_ip: str = "192.168.50.2",
    agent_git_gate_host: str = "127.0.0.1:55555",
    agent_supervise_url: str = "http://127.0.0.1:55556/",
    codex_auth_file: Path | None = None,
    agent_provider_template: str = "claude",
    guest_env: dict[str, str] | None = None,
) -> SmolmachinesBottlePlan:
    bottle_json: dict = {}
    git_gate_json: dict = {}
    if git:
        git_gate_json["repos"] = {
            g.Name: {
                "url": g.Upstream,
                "identity": g.IdentityFile,
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
        machine_name="bot-bottle-demo-abc12",
        agent_image_ref="bot-bottle-claude:latest",
        guest_env=dict(guest_env or {}),
        prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
        proxy_plan=PipelockProxyPlan(
            yaml_path=Path("/tmp/pipelock.yaml"),
            slug="demo-abc12",
            ca_cert_host_path=pipelock_ca_path,
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
        workspace_plan=workspace_plan(spec, guest_home="/home/node"),
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
            image="",
            dockerfile="",
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
        guest_env=dict(guest_env or {}),
        dirs=(AgentProvisionDir(auth_dir),),
        files=tuple(files),
        pre_copy=pre_copy,
        verify=verify,
    )


class TestProvisionPrompt(unittest.TestCase):
    def test_cp_uses_bottle_cp_in(self):
        bottle = _make_bottle()
        _prompt.provision_prompt(_plan(), bottle)
        bottle.cp_in.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "/home/node/.bot-bottle-prompt.txt",
        )

    def test_returns_path_when_agent_has_prompt(self):
        bottle = _make_bottle()
        r = _prompt.provision_prompt(
            _plan(agent_prompt="You are a helpful assistant."),
            bottle,
        )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)

    def test_returns_none_when_agent_has_no_prompt(self):
        # The file is still copied (path-must-exist contract);
        # only the return value differs.
        bottle = _make_bottle()
        r = _prompt.provision_prompt(_plan(agent_prompt=""), bottle)
        self.assertIsNone(r)
        bottle.cp_in.assert_called_once()

    def test_chowns_to_node_after_copy(self):
        # cp_in lands as root; without the chown, the node user
        # can't read its own mode-600 prompt.
        bottle = _make_bottle()
        _prompt.provision_prompt(_plan(), bottle)
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("chown node:node" in s and "/home/node/.bot-bottle-prompt.txt" in s
                for s in scripts)
        )
        self.assertTrue(
            any("chmod 600" in s and "/home/node/.bot-bottle-prompt.txt" in s
                for s in scripts)
        )


class TestProvisionProviderAuth(unittest.TestCase):
    def test_noop_for_non_codex_provider(self):
        bottle = _make_bottle()
        _provider_auth.provision_provider_auth(_plan(), bottle)
        self.assertEqual(0, bottle.cp_in.call_count)
        self.assertEqual(0, bottle.exec.call_count)

    def test_codex_provider_trusts_launch_dir_without_auth_file(self):
        bottle = _make_bottle()
        _provider_auth.provision_provider_auth(
            _plan(agent_provider_template="codex"),
            bottle,
        )
        bottle.cp_in.assert_called_once_with(
            "/tmp/codex-config.toml",
            "/home/node/.codex/config.toml",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("mkdir -p" in s and "/home/node/.codex" in s for s in scripts))
        self.assertTrue(
            any("chown" in s and "node:node" in s and "/home/node/.codex/config.toml" in s
                for s in scripts)
        )
        self.assertTrue(
            any("chmod" in s and "600" in s and "/home/node/.codex/config.toml" in s
                for s in scripts)
        )

    def test_copies_dummy_auth_json_to_codex_home(self):
        bottle = _make_bottle()
        _provider_auth.provision_provider_auth(
            _plan(
                agent_provider_template="codex",
                codex_auth_file=Path("/tmp/codex-auth.json"),
            ),
            bottle,
        )
        cp_calls = [c.args for c in bottle.cp_in.call_args_list]
        self.assertIn(
            ("/tmp/codex-config.toml", "/home/node/.codex/config.toml"),
            cp_calls,
        )
        self.assertIn(
            ("/tmp/codex-auth.json", "/home/node/.codex/auth.json"),
            cp_calls,
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("mkdir -p" in s and "/home/node/.codex" in s for s in scripts))
        self.assertTrue(
            any("chown" in s and "node:node" in s and s.rstrip().endswith("/home/node/.codex")
                for s in scripts)
        )
        self.assertTrue(
            any("chmod" in s and "700" in s and s.rstrip().endswith("/home/node/.codex")
                for s in scripts)
        )
        # The pre_copy `find ... -delete` script should be present
        # (shlex.join properly quotes the `(`/`)`/`*.sqlite`).
        self.assertTrue(
            any("find" in s and "-delete" in s and "*.sqlite" in s for s in scripts)
        )
        self.assertTrue(
            any("chown" in s and "/home/node/.codex/auth.json" in s for s in scripts)
        )
        self.assertTrue(
            any("chmod" in s and "600" in s and "/home/node/.codex/auth.json" in s
                for s in scripts)
        )
        # Verify command runs `codex login status` via runuser node.
        self.assertTrue(
            any("runuser" in s and "codex login status" in s for s in scripts)
        )

    def test_honors_codex_home_from_guest_env(self):
        bottle = _make_bottle()
        _provider_auth.provision_provider_auth(
            _plan(
                agent_provider_template="codex",
                codex_auth_file=Path("/tmp/codex-auth.json"),
                guest_env={"CODEX_HOME": "/run/codex-home"},
            ),
            bottle,
        )
        cp_calls = [c.args for c in bottle.cp_in.call_args_list]
        self.assertIn(
            ("/tmp/codex-config.toml", "/run/codex-home/config.toml"),
            cp_calls,
        )
        self.assertIn(
            ("/tmp/codex-auth.json", "/run/codex-home/auth.json"),
            cp_calls,
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("runuser" in s and "CODEX_HOME=/run/codex-home" in s and "codex login status" in s
                for s in scripts)
        )

    def test_dies_when_codex_home_cannot_be_created(self):
        bottle = _make_bottle(
            exec_result=ExecResult(1, "", "mkdir: nope\n"),
        )
        with self.assertRaises(SystemExit):
            _provider_auth.provision_provider_auth(
                _plan(
                    agent_provider_template="codex",
                    codex_auth_file=Path("/tmp/codex-auth.json"),
                ),
                bottle,
            )
        self.assertEqual(0, bottle.cp_in.call_count)
        self.assertEqual(1, bottle.exec.call_count)

    def test_dies_when_codex_rejects_dummy_auth(self):
        # CODEX_HOME setup ok, but codex login status fails (last exec).
        bottle = _make_bottle()
        bottle.exec.side_effect = [
            ExecResult(0, "", ""),  # mkdir CODEX_HOME
            ExecResult(0, "", ""),  # chown CODEX_HOME
            ExecResult(0, "", ""),  # chmod CODEX_HOME
            ExecResult(0, "", ""),  # find ... -delete (pre_copy)
            ExecResult(0, "", ""),  # chown config.toml
            ExecResult(0, "", ""),  # chmod config.toml
            ExecResult(0, "", ""),  # chown auth.json
            ExecResult(0, "", ""),  # chmod auth.json
            ExecResult(1, "Not logged in\n", ""),  # login status (verify)
        ]
        with self.assertRaises(SystemExit):
            _provider_auth.provision_provider_auth(
                _plan(
                    agent_provider_template="codex",
                    codex_auth_file=Path("/tmp/codex-auth.json"),
                ),
                bottle,
            )


class TestProvisionSkills(unittest.TestCase):
    def _patch_host_skill_dir(self, returns: dict[str, str]):
        return patch(
            "bot_bottle.backend.smolmachines.provision.skills.host_skill_dir",
            side_effect=lambda n: returns.get(n, f"/nope/{n}"),
        )

    def test_no_op_when_agent_has_no_skills(self):
        bottle = _make_bottle()
        _skills.provision_skills(_plan(skills=[]), bottle)
        self.assertEqual(0, bottle.cp_in.call_count)
        self.assertEqual(0, bottle.exec.call_count)

    def test_mkdir_plus_cp_per_skill(self):
        bottle = _make_bottle()
        with self._patch_host_skill_dir({
            "init-prd": "/host/skills/init-prd",
            "verify": "/host/skills/verify",
        }), patch(
            "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
            return_value=True,
        ):
            _skills.provision_skills(
                _plan(skills=["init-prd", "verify"]),
                bottle,
            )

        # mkdir skills_dir once + (rm -rf + chown) per skill = 5 exec calls.
        self.assertEqual(5, bottle.exec.call_count)
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("mkdir -p" in s and "/home/node/.claude/skills" in s for s in scripts)
        )
        # Two cp calls, one per skill, into the per-skill subdir.
        self.assertEqual(2, bottle.cp_in.call_count)
        cp_targets = {call.args[1] for call in bottle.cp_in.call_args_list}
        self.assertEqual(
            {
                "/home/node/.claude/skills/init-prd",
                "/home/node/.claude/skills/verify",
            },
            cp_targets,
        )
        # Each skill gets a chown -R node:node so claude can read it.
        chown_scripts = [s for s in scripts if "chown -R node:node" in s]
        self.assertEqual(2, len(chown_scripts))

    def test_skills_dir_overridable_via_env(self):
        import os
        bottle = _make_bottle()
        with self._patch_host_skill_dir({"init-prd": "/host/skills/init-prd"}), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=True,
             ), \
             patch.dict(os.environ, {"BOT_BOTTLE_GUEST_SKILLS_DIR": "/home/node/.claude/skills"}):
            _skills.provision_skills(_plan(skills=["init-prd"]), bottle)
        self.assertEqual(
            "/home/node/.claude/skills/init-prd",
            bottle.cp_in.call_args.args[1],
        )

    def test_missing_skill_dies(self):
        bottle = _make_bottle()
        with self._patch_host_skill_dir({"init-prd": "/host/skills/init-prd"}), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=False,
             ):
            with self.assertRaises(SystemExit):
                _skills.provision_skills(_plan(skills=["init-prd"]), bottle)


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
    """provision_ca selects the right CA cert (egress when the
    bottle has routes, else pipelock) and dispatches
    cp_in + exec in the right order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-ca.")
        self.tmp = Path(self._tmp.name)
        self.pipelock_ca = self.tmp / "pipelock-ca.pem"
        self.egress_ca = self.tmp / "egress-ca.pem"
        _write_self_signed_cert(self.pipelock_ca)
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

    def test_pipelock_path_when_no_routes(self):
        plan = _plan(pipelock_ca_path=self.pipelock_ca)
        bottle = _make_bottle(exec_result=self._UPDATE_OK)
        _ca.provision_ca(plan, bottle)
        bottle.cp_in.assert_called_once_with(
            str(self.pipelock_ca),
            _ca.AGENT_CA_PATH,
        )
        # chmod + chown + update-ca-certificates are folded into
        # one exec invocation; look at the single exec's script
        # rather than expecting separate calls.
        bottle.exec.assert_called_once()
        script = bottle.exec.call_args.args[0]
        self.assertIn("chmod 644", script)
        self.assertIn("update-ca-certificates", script)
        self.assertEqual("root", bottle.exec.call_args.kwargs.get("user"))

    def test_egress_path_when_routes_declared(self):
        plan = _plan(
            egress_routes=(EgressRoute(host="api.anthropic.com"),),
            egress_ca_path=self.egress_ca,
            pipelock_ca_path=self.pipelock_ca,
        )
        bottle = _make_bottle(exec_result=self._UPDATE_OK)
        _ca.provision_ca(plan, bottle)
        # When routes are declared, egress is the agent's first hop,
        # so egress's CA is the one that gets installed.
        bottle.cp_in.assert_called_once_with(
            str(self.egress_ca),
            _ca.AGENT_CA_PATH,
        )

    def test_retries_smolvm_sigkill_during_update_ca(self):
        plan = _plan(pipelock_ca_path=self.pipelock_ca)
        killed = ExecResult(
            returncode=137,
            stdout="Updating certificates in /etc/ssl/certs...\n",
            stderr="",
        )
        bottle = _make_bottle()
        bottle.exec.side_effect = [killed, self._UPDATE_OK]
        with patch(
            "bot_bottle.backend.smolmachines.provision.ca.time.sleep"
        ) as sleep:
            _ca.provision_ca(plan, bottle)

        self.assertEqual(2, bottle.exec.call_count)
        sleep.assert_called_once_with(1.0)

    def test_dies_when_selected_cert_missing(self):
        # Plan claims a pipelock cert at a path that doesn't exist —
        # something went wrong in launch's pipelock_tls_init.
        plan = _plan(pipelock_ca_path=self.tmp / "does-not-exist.pem")
        bottle = _make_bottle()
        with self.assertRaises(SystemExit):
            _ca.provision_ca(plan, bottle)


class TestProvisionGit(unittest.TestCase):
    """provision_git dispatches two independent passes (cwd .git
    copy + gitconfig insteadOf write); each no-ops on its own
    when its condition doesn't hold."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-git.")
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_no_cwd_and_no_git_entries(self):
        bottle = _make_bottle()
        _git.provision_git(_plan(stage_dir=self.stage), bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_copies_cwd_git_when_copy_cwd_and_git_present(self):
        # Stage a fake host .git dir under user_cwd so the path-
        # check in _provision_cwd_git fires.
        cwd = self.stage / "cwd"
        (cwd / ".git").mkdir(parents=True)
        plan = _plan(
            copy_cwd=True, user_cwd=str(cwd), stage_dir=self.stage,
        )
        bottle = _make_bottle()
        _git.provision_git(plan, bottle)
        bottle.cp_in.assert_called_once_with(
            f"{cwd}/.git",
            "/home/node/workspace/.git",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(any("mkdir -p" in s and "/home/node/workspace" in s for s in scripts))
        # chown the workspace tree so the agent (node) owns it.
        self.assertTrue(
            any("chown -R" in s and "node:node" in s and "/home/node/workspace/.git" in s
                for s in scripts)
        )

    def test_skips_cwd_when_copy_cwd_false(self):
        plan = _plan(copy_cwd=False, stage_dir=self.stage)
        bottle = _make_bottle()
        _git.provision_git(plan, bottle)
        bottle.cp_in.assert_not_called()

    def test_writes_gitconfig_with_ip_port_form_for_smolmachines(self):
        # Smolmachines's TSI-allowlisted guest dials git-gate via
        # smart HTTP at `127.0.0.1:<host port>` — the bundle's
        # git HTTP port is published on host loopback at launch
        # time, and the plan carries the discovered host port.
        plan = _plan(
            git=[GitEntry(
                Name="bot-bottle",
                Upstream="ssh://git@host/repo.git",
                IdentityFile="~/.ssh/id_ed25519",
            )],
            stage_dir=self.stage,
            agent_git_gate_host="127.0.0.1:9418",
        )
        bottle = _make_bottle()
        _git.provision_git(plan, bottle)
        # The staged gitconfig path is whatever NamedTemporaryFile
        # picked; we read its contents.
        cp_call = bottle.cp_in.call_args
        staged_path = Path(cp_call.args[0])
        self.assertEqual(self.stage, staged_path.parent)
        content = staged_path.read_text()
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
            "egress,pipelock,git-gate,git-http",
            spec.daemons_csv,
        )
        self.assertIn(9420, spec.ports_to_publish)
        self.assertNotIn(9418, spec.ports_to_publish)


class TestProvisionGitUser(unittest.TestCase):
    """`_provision_git_user` runs `git config --global` inside the
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
        _git._provision_git_user(_plan(), bottle)
        self.assertEqual([], self._git_config_calls(bottle))

    def test_sets_name_and_email_as_node(self):
        plan = _plan(git_user={
            "name": "Eric Bauerfeld",
            "email": "eric@dideric.is",
        })
        bottle = _make_bottle()
        _git._provision_git_user(plan, bottle)
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
        _git._provision_git_user(plan, bottle)
        calls = self._git_config_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.name", calls[0][0])
        self.assertIn("Bot", calls[0][0])

    def test_email_only(self):
        plan = _plan(git_user={"email": "bot@example.com"})
        bottle = _make_bottle()
        _git._provision_git_user(plan, bottle)
        calls = self._git_config_calls(bottle)
        self.assertEqual(1, len(calls))
        self.assertIn("user.email", calls[0][0])
        self.assertIn("bot@example.com", calls[0][0])


class TestProvisionWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-workspace.")
        self.stage = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_copy_cwd_false(self):
        plan = _plan(copy_cwd=False, stage_dir=self.stage)
        bottle = _make_bottle()
        _workspace.provision_workspace(plan, bottle)
        bottle.cp_in.assert_not_called()
        bottle.exec.assert_not_called()

    def test_copies_workspace_to_plan_path_and_chowns(self):
        cwd = self.stage / "cwd"
        cwd.mkdir()
        plan = _plan(copy_cwd=True, user_cwd=str(cwd), stage_dir=self.stage)
        bottle = _make_bottle()
        _workspace.provision_workspace(plan, bottle)

        bottle.cp_in.assert_called_once_with(
            str(cwd),
            "/home/node/workspace",
        )
        scripts = _exec_scripts(bottle)
        self.assertTrue(
            any("rm -rf /home/node/workspace" in s and "mkdir -p /home/node" in s
                for s in scripts)
        )
        self.assertTrue(
            any("chown -R node:node /home/node/workspace" in s
                and "chmod 755 /home/node/workspace" in s
                for s in scripts)
        )


class TestProvisionSupervise(unittest.TestCase):
    def test_noop_when_supervise_not_enabled(self):
        bottle = _make_bottle()
        _supervise.provision_supervise(_plan(), bottle)
        bottle.exec.assert_not_called()

    def test_calls_claude_mcp_add_when_supervise_enabled(self):
        plan = _plan(
            supervise=True,
            agent_supervise_url="http://127.0.0.1:9100/",
        )
        bottle = _make_bottle()
        _supervise.provision_supervise(plan, bottle)
        bottle.exec.assert_called_once()
        script = bottle.exec.call_args.args[0]
        user = bottle.exec.call_args.kwargs.get("user")
        self.assertEqual("node", user)
        # SmolmachinesBottle.exec(user="node") handles uid switch +
        # HOME setup automatically — the script itself is just the
        # claude command.
        self.assertIn("claude mcp add", script)
        self.assertIn("--scope user", script)
        self.assertIn("--transport http", script)
        self.assertIn("supervise", script)
        self.assertIn("http://127.0.0.1:9100/", script)

    def test_non_zero_exit_logs_warning_but_does_not_raise(self):
        plan = _plan(supervise=True)
        bottle = _make_bottle(
            exec_result=ExecResult(returncode=1, stdout="", stderr="boom"),
        )
        # No raise — the bottle still works without the MCP
        # entry, so we log and move on.
        _supervise.provision_supervise(plan, bottle)


if __name__ == "__main__":
    unittest.main()
