"""Unit: smolmachines provisioning helpers (PRD 0023 chunks 4a + 4d).

Tests mock `smolvm.machine_cp` / `smolvm.machine_exec` and assert
on the dispatched call shape. The real round-trip lives in the
chunk-4 integration smoke."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend import BottleSpec
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
)
from bot_bottle.backend.smolmachines.launch import _bundle_launch_spec
from bot_bottle.backend.smolmachines.smolvm import SmolvmRunResult
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream
from bot_bottle.manifest import GitEntry, Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.supervise import SupervisePlan


def _remote_host(g: GitEntry) -> str:
    if g.UpstreamHost:
        return g.UpstreamHost
    return g.Upstream.split("@", 1)[1].split("/", 1)[0].split(":", 1)[0]


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
) -> SmolmachinesBottlePlan:
    bottle_json: dict = {}
    git_json: dict = {}
    if git:
        git_json["remotes"] = {
            _remote_host(g): {
                "Name": g.Name,
                "Upstream": g.Upstream,
                "IdentityFile": g.IdentityFile,
            }
            for g in git
        }
    if git_user is not None:
        git_json["user"] = git_user
    if git_json:
        bottle_json["git"] = git_json
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
        guest_env={},
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
        codex_auth_file=codex_auth_file,
    )


class TestProvisionPrompt(unittest.TestCase):
    def test_cp_uses_smolvm_machine_cp_with_machine_path_syntax(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            _prompt.provision_prompt(_plan(), "bot-bottle-demo-abc12")
        cp.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "bot-bottle-demo-abc12:/home/node/.bot-bottle-prompt.txt",
        )

    def test_returns_path_when_agent_has_prompt(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ), patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            r = _prompt.provision_prompt(
                _plan(agent_prompt="You are a helpful assistant."),
                "bot-bottle-demo-abc12",
            )
        self.assertEqual("/home/node/.bot-bottle-prompt.txt", r)

    def test_returns_none_when_agent_has_no_prompt(self):
        # The file is still copied (path-must-exist contract);
        # only the return value differs.
        with patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            r = _prompt.provision_prompt(_plan(agent_prompt=""), "bot-bottle-demo-abc12")
        self.assertIsNone(r)
        cp.assert_called_once()

    def test_chowns_to_node_after_copy(self):
        # machine cp lands as root; without the chown, the node user
        # can't read its own mode-600 prompt.
        with patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ), patch(
            "bot_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ) as ex:
            _prompt.provision_prompt(_plan(), "bot-bottle-demo-abc12")
        argv_seen = [call.args[1] for call in ex.call_args_list]
        self.assertIn(
            ["chown", "node:node", "/home/node/.bot-bottle-prompt.txt"],
            argv_seen,
        )
        self.assertIn(
            ["chmod", "600", "/home/node/.bot-bottle-prompt.txt"],
            argv_seen,
        )


class TestProvisionProviderAuth(unittest.TestCase):
    def test_noop_without_codex_auth_file(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.provider_auth._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.provider_auth._smolvm.machine_exec"
        ) as ex:
            _provider_auth.provision_provider_auth(
                _plan(), "bot-bottle-demo-abc12",
            )
        self.assertEqual(0, cp.call_count)
        self.assertEqual(0, ex.call_count)

    def test_copies_dummy_auth_json_to_codex_home(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.provider_auth._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.provider_auth._smolvm.machine_exec"
        ) as ex:
            _provider_auth.provision_provider_auth(
                _plan(codex_auth_file=Path("/tmp/codex-auth.json")),
                "bot-bottle-demo-abc12",
            )
        cp.assert_called_once_with(
            "/tmp/codex-auth.json",
            "bot-bottle-demo-abc12:/home/node/.codex/auth.json",
        )
        argv_seen = [call.args[1] for call in ex.call_args_list]
        self.assertIn(["mkdir", "-p", "/home/node/.codex"], argv_seen)
        self.assertIn(
            ["chown", "node:node", "/home/node/.codex/auth.json"],
            argv_seen,
        )
        self.assertIn(["chmod", "600", "/home/node/.codex/auth.json"], argv_seen)


class TestProvisionSkills(unittest.TestCase):
    def _patch_host_skill_dir(self, returns: dict[str, str]):
        return patch(
            "bot_bottle.backend.smolmachines.provision.skills.host_skill_dir",
            side_effect=lambda n: returns.get(n, f"/nope/{n}"),
        )

    def test_no_op_when_agent_has_no_skills(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
        ) as ex:
            _skills.provision_skills(_plan(skills=[]), "bot-bottle-demo-abc12")
        self.assertEqual(0, cp.call_count)
        self.assertEqual(0, ex.call_count)

    def test_mkdir_plus_cp_per_skill(self):
        with self._patch_host_skill_dir({
            "init-prd": "/host/skills/init-prd",
            "verify": "/host/skills/verify",
        }), patch(
            "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
            return_value=True,
        ), patch(
            "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
        ) as ex:
            _skills.provision_skills(
                _plan(skills=["init-prd", "verify"]),
                "bot-bottle-demo-abc12",
            )

        # mkdir -p once + (rm -rf + chown) per skill = 5 exec calls.
        self.assertEqual(5, ex.call_count)
        mkdir_call = ex.call_args_list[0]
        self.assertEqual(
            ("bot-bottle-demo-abc12", ["mkdir", "-p", "/home/node/.claude/skills"]),
            mkdir_call.args,
        )
        # Two cp calls, one per skill, into the per-skill subdir.
        self.assertEqual(2, cp.call_count)
        cp_targets = {call.args[1] for call in cp.call_args_list}
        self.assertEqual(
            {
                "bot-bottle-demo-abc12:/home/node/.claude/skills/init-prd",
                "bot-bottle-demo-abc12:/home/node/.claude/skills/verify",
            },
            cp_targets,
        )
        # Each skill gets a chown -R node:node so claude can read it.
        chown_argvs = [
            call.args[1] for call in ex.call_args_list
            if call.args[1][:1] == ["chown"]
        ]
        self.assertEqual(2, len(chown_argvs))
        chown_targets = {argv[-1] for argv in chown_argvs}
        self.assertEqual(
            {
                "/home/node/.claude/skills/init-prd",
                "/home/node/.claude/skills/verify",
            },
            chown_targets,
        )

    def test_skills_dir_overridable_via_env(self):
        import os
        with self._patch_host_skill_dir({"init-prd": "/host/skills/init-prd"}), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=True,
             ), \
             patch.dict(os.environ, {"BOT_BOTTLE_GUEST_SKILLS_DIR": "/home/node/.claude/skills"}), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
             ) as cp, \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
             ):
            _skills.provision_skills(_plan(skills=["init-prd"]), "bot-bottle-demo-abc12")
        self.assertEqual(
            "bot-bottle-demo-abc12:/home/node/.claude/skills/init-prd",
            cp.call_args.args[1],
        )

    def test_missing_skill_dies(self):
        with self._patch_host_skill_dir({"init-prd": "/host/skills/init-prd"}), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=False,
             ), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
             ), \
             patch(
                 "bot_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
             ):
            with self.assertRaises(SystemExit):
                _skills.provision_skills(_plan(skills=["init-prd"]), "bot-bottle-demo-abc12")


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
    machine_cp + machine_exec in the right order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-prov-ca.")
        self.tmp = Path(self._tmp.name)
        self.pipelock_ca = self.tmp / "pipelock-ca.pem"
        self.egress_ca = self.tmp / "egress-ca.pem"
        _write_self_signed_cert(self.pipelock_ca)
        _write_self_signed_cert(self.egress_ca)

    def tearDown(self):
        self._tmp.cleanup()

    # provision_ca dies hard if update-ca-certificates' stdout
    # doesn't include "1 added"; supply a stock success return
    # so the bulk of the tests below exercise the happy path.
    _UPDATE_OK = SmolvmRunResult(
        returncode=0,
        stdout="Updating certificates in /etc/ssl/certs...\n1 added, 0 removed; done.\n",
        stderr="",
    )

    def test_pipelock_path_when_no_routes(self):
        plan = _plan(pipelock_ca_path=self.pipelock_ca)
        with patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec",
            return_value=self._UPDATE_OK,
        ) as ex:
            _ca.provision_ca(plan, "bot-bottle-demo-abc12")
        cp.assert_called_once_with(
            str(self.pipelock_ca),
            "bot-bottle-demo-abc12:" + _ca.AGENT_CA_PATH,
        )
        # chmod + chown + update-ca-certificates are now folded
        # into one `sh -c` invocation (working around a smolvm
        # exec warm-up SIGKILL race), so we look at the single
        # exec's argv rather than expecting separate calls.
        ex.assert_called_once()
        argv = ex.call_args.args[1]
        self.assertEqual("sh", argv[0])
        self.assertEqual("-c", argv[1])
        self.assertIn("chmod 644", argv[2])
        self.assertIn("update-ca-certificates", argv[2])

    def test_egress_path_when_routes_declared(self):
        plan = _plan(
            egress_routes=(EgressRoute(host="api.anthropic.com"),),
            egress_ca_path=self.egress_ca,
            pipelock_ca_path=self.pipelock_ca,
        )
        with patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec",
            return_value=self._UPDATE_OK,
        ):
            _ca.provision_ca(plan, "bot-bottle-demo-abc12")
        # When routes are declared, egress is the agent's first hop,
        # so egress's CA is the one that gets installed.
        cp.assert_called_once_with(
            str(self.egress_ca),
            "bot-bottle-demo-abc12:" + _ca.AGENT_CA_PATH,
        )

    def test_dies_when_selected_cert_missing(self):
        # Plan claims a pipelock cert at a path that doesn't exist —
        # something went wrong in launch's pipelock_tls_init.
        plan = _plan(pipelock_ca_path=self.tmp / "does-not-exist.pem")
        with patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ), patch(
            "bot_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec"
        ):
            with self.assertRaises(SystemExit):
                _ca.provision_ca(plan, "bot-bottle-demo-abc12")


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
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git.provision_git(
                _plan(stage_dir=self.stage), "bot-bottle-demo-abc12",
            )
        cp.assert_not_called()
        ex.assert_not_called()

    def test_copies_cwd_git_when_copy_cwd_and_git_present(self):
        # Stage a fake host .git dir under user_cwd so the path-
        # check in _provision_cwd_git fires.
        cwd = self.stage / "cwd"
        (cwd / ".git").mkdir(parents=True)
        plan = _plan(
            copy_cwd=True, user_cwd=str(cwd), stage_dir=self.stage,
        )
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git.provision_git(plan, "bot-bottle-demo-abc12")
        cp.assert_called_once_with(
            f"{cwd}/.git",
            "bot-bottle-demo-abc12:/home/node/workspace/.git",
        )
        argvs = [c.args[1] for c in ex.call_args_list]
        self.assertIn(["mkdir", "-p", "/home/node/workspace"], argvs)
        # chown the workspace tree so the agent (node) owns it.
        self.assertIn(
            ["chown", "-R", "node:node", "/home/node/workspace/.git"],
            argvs,
        )

    def test_skips_cwd_when_copy_cwd_false(self):
        plan = _plan(copy_cwd=False, stage_dir=self.stage)
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ):
            _git.provision_git(plan, "bot-bottle-demo-abc12")
        cp.assert_not_called()

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
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ):
            _git.provision_git(plan, "bot-bottle-demo-abc12")
        # The staged gitconfig path is whatever NamedTemporaryFile
        # picked; we read its contents.
        cp_call = cp.call_args
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
    guest as the node user with HOME forced via `smolvm -e`
    (otherwise --global lands in /root/.gitconfig). No-op when the
    bottle didn't declare git_user (issue #86)."""

    def _git_config_calls(self, mock_exec):
        """Filter machine_exec calls down to git-config invocations,
        return list of (argv, env-dict) tuples."""
        out = []
        for c in mock_exec.call_args_list:
            argv = c.args[1] if len(c.args) > 1 else c.kwargs.get("argv", [])
            if "git" in argv and "config" in argv:
                out.append((argv, c.kwargs.get("env") or {}))
        return out

    def test_noop_when_no_git_user(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git._provision_git_user(_plan(), "bot-bottle-demo-abc12")
        self.assertEqual([], self._git_config_calls(ex))

    def test_sets_name_and_email_as_node(self):
        plan = _plan(git_user={
            "name": "Eric Bauerfeld",
            "email": "eric@dideric.is",
        })
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git._provision_git_user(plan, "bot-bottle-demo-abc12")
        calls = self._git_config_calls(ex)
        self.assertEqual(2, len(calls))
        # Both go through `runuser -u node --` so they run as node;
        # HOME is forced via smolvm -e so --global writes to
        # /home/node/.gitconfig and not /root/.gitconfig.
        for argv, env in calls:
            self.assertEqual(
                ["runuser", "-u", "node", "--",
                 "git", "config", "--global"],
                argv[:7],
            )
            self.assertEqual("/home/node", env.get("HOME"))
            self.assertEqual("node", env.get("USER"))
        self.assertEqual(["user.name", "Eric Bauerfeld"], calls[0][0][7:])
        self.assertEqual(["user.email", "eric@dideric.is"], calls[1][0][7:])

    def test_name_only(self):
        plan = _plan(git_user={"name": "Bot"})
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git._provision_git_user(plan, "bot-bottle-demo-abc12")
        calls = self._git_config_calls(ex)
        self.assertEqual(1, len(calls))
        self.assertEqual(["user.name", "Bot"], calls[0][0][7:])

    def test_email_only(self):
        plan = _plan(git_user={"email": "bot@example.com"})
        with patch(
            "bot_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git._provision_git_user(plan, "bot-bottle-demo-abc12")
        calls = self._git_config_calls(ex)
        self.assertEqual(1, len(calls))
        self.assertEqual(["user.email", "bot@example.com"], calls[0][0][7:])


class TestProvisionSupervise(unittest.TestCase):
    def test_noop_when_supervise_not_enabled(self):
        with patch(
            "bot_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec"
        ) as ex:
            _supervise.provision_supervise(_plan(), "bot-bottle-demo-abc12")
        ex.assert_not_called()

    def test_calls_claude_mcp_add_when_supervise_enabled(self):
        plan = _plan(
            supervise=True,
            agent_supervise_url="http://127.0.0.1:9100/",
        )
        with patch(
            "bot_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec",
            return_value=SmolvmRunResult(returncode=0, stdout="", stderr=""),
        ) as ex:
            _supervise.provision_supervise(plan, "bot-bottle-demo-abc12")
        ex.assert_called_once()
        argv = ex.call_args.args[1]
        # `claude mcp add --scope user` writes to ~/.claude.json,
        # and the agent is the `node` user — switch UID + set
        # HOME so the config lands in /home/node/.claude.json,
        # not root's. URL is the agent-side endpoint (host
        # loopback + discovered port), not the docker bridge IP.
        self.assertEqual(
            [
                "runuser", "-u", "node", "--",
                "env", "HOME=/home/node",
                "claude", "mcp", "add",
                "--scope", "user",
                "--transport", "http",
                "supervise",
                "http://127.0.0.1:9100/",
            ],
            argv,
        )

    def test_non_zero_exit_logs_warning_but_does_not_raise(self):
        plan = _plan(supervise=True)
        with patch(
            "bot_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec",
            return_value=SmolvmRunResult(
                returncode=1, stdout="", stderr="boom",
            ),
        ):
            # No raise — the bottle still works without the MCP
            # entry, so we log and move on.
            _supervise.provision_supervise(plan, "bot-bottle-demo-abc12")


if __name__ == "__main__":
    unittest.main()
