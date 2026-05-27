"""Unit: smolmachines provisioning helpers (PRD 0023 chunks 4a + 4d).

Tests mock `smolvm.machine_cp` / `smolvm.machine_exec` and assert
on the dispatched call shape. The real round-trip lives in the
chunk-4 integration smoke."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.backend import BottleSpec
from claude_bottle.backend.smolmachines.bottle_plan import (
    SmolmachinesBottlePlan,
)
from claude_bottle.backend.smolmachines.provision import (
    ca as _ca,
    git as _git,
    prompt as _prompt,
    skills as _skills,
    supervise as _supervise,
)
from claude_bottle.backend.smolmachines.smolvm import SmolvmRunResult
from claude_bottle.egress import EgressPlan, EgressRoute
from claude_bottle.git_gate import GitGatePlan
from claude_bottle.manifest import GitEntry, Manifest
from claude_bottle.pipelock import PipelockProxyPlan
from claude_bottle.supervise import SupervisePlan


def _plan(
    *,
    agent_prompt: str = "",
    skills: list[str] | None = None,
    git: list[GitEntry] = (),
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
) -> SmolmachinesBottlePlan:
    bottle_json: dict = {}
    if git:
        bottle_json["git"] = [
            {
                "Name": g.Name,
                "Upstream": g.Upstream,
                "IdentityFile": g.IdentityFile,
            }
            for g in git
        ]
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
        machine_name="claude-bottle-demo-abc12",
        agent_from_path=Path("/tmp/agent.smolmachine"),
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
    )


class TestProvisionPrompt(unittest.TestCase):
    def test_cp_uses_smolvm_machine_cp_with_machine_path_syntax(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            _prompt.provision_prompt(_plan(), "claude-bottle-demo-abc12")
        cp.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "claude-bottle-demo-abc12:/home/node/.claude-bottle-prompt.txt",
        )

    def test_returns_path_when_agent_has_prompt(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ), patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            r = _prompt.provision_prompt(
                _plan(agent_prompt="You are a helpful assistant."),
                "claude-bottle-demo-abc12",
            )
        self.assertEqual("/home/node/.claude-bottle-prompt.txt", r)

    def test_returns_none_when_agent_has_no_prompt(self):
        # The file is still copied (path-must-exist contract);
        # only the return value differs.
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ):
            r = _prompt.provision_prompt(_plan(agent_prompt=""), "claude-bottle-demo-abc12")
        self.assertIsNone(r)
        cp.assert_called_once()

    def test_chowns_to_node_after_copy(self):
        # machine cp lands as root; without the chown, the node user
        # can't read its own mode-600 prompt.
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ), patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_exec"
        ) as ex:
            _prompt.provision_prompt(_plan(), "claude-bottle-demo-abc12")
        argv_seen = [call.args[1] for call in ex.call_args_list]
        self.assertIn(
            ["chown", "node:node", "/home/node/.claude-bottle-prompt.txt"],
            argv_seen,
        )
        self.assertIn(
            ["chmod", "600", "/home/node/.claude-bottle-prompt.txt"],
            argv_seen,
        )


class TestProvisionSkills(unittest.TestCase):
    def _patch_host_skill_dir(self, returns: dict[str, str]):
        return patch(
            "claude_bottle.backend.smolmachines.provision.skills.host_skill_dir",
            side_effect=lambda n: returns.get(n, f"/nope/{n}"),
        )

    def test_no_op_when_agent_has_no_skills(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
        ) as ex:
            _skills.provision_skills(_plan(skills=[]), "claude-bottle-demo-abc12")
        self.assertEqual(0, cp.call_count)
        self.assertEqual(0, ex.call_count)

    def test_mkdir_plus_cp_per_skill(self):
        with self._patch_host_skill_dir({
            "init-prd": "/host/skills/init-prd",
            "verify": "/host/skills/verify",
        }), patch(
            "claude_bottle.backend.smolmachines.provision.skills.os.path.isdir",
            return_value=True,
        ), patch(
            "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
        ) as ex:
            _skills.provision_skills(
                _plan(skills=["init-prd", "verify"]),
                "claude-bottle-demo-abc12",
            )

        # mkdir -p once + (rm -rf + chown) per skill = 5 exec calls.
        self.assertEqual(5, ex.call_count)
        mkdir_call = ex.call_args_list[0]
        self.assertEqual(
            ("claude-bottle-demo-abc12", ["mkdir", "-p", "/home/node/.claude/skills"]),
            mkdir_call.args,
        )
        # Two cp calls, one per skill, into the per-skill subdir.
        self.assertEqual(2, cp.call_count)
        cp_targets = {call.args[1] for call in cp.call_args_list}
        self.assertEqual(
            {
                "claude-bottle-demo-abc12:/home/node/.claude/skills/init-prd",
                "claude-bottle-demo-abc12:/home/node/.claude/skills/verify",
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
                 "claude_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=True,
             ), \
             patch.dict(os.environ, {"CLAUDE_BOTTLE_GUEST_SKILLS_DIR": "/home/node/.claude/skills"}), \
             patch(
                 "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
             ) as cp, \
             patch(
                 "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
             ):
            _skills.provision_skills(_plan(skills=["init-prd"]), "claude-bottle-demo-abc12")
        self.assertEqual(
            "claude-bottle-demo-abc12:/home/node/.claude/skills/init-prd",
            cp.call_args.args[1],
        )

    def test_missing_skill_dies(self):
        with self._patch_host_skill_dir({"init-prd": "/host/skills/init-prd"}), \
             patch(
                 "claude_bottle.backend.smolmachines.provision.skills.os.path.isdir",
                 return_value=False,
             ), \
             patch(
                 "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_cp"
             ), \
             patch(
                 "claude_bottle.backend.smolmachines.provision.skills._smolvm.machine_exec"
             ):
            with self.assertRaises(SystemExit):
                _skills.provision_skills(_plan(skills=["init-prd"]), "claude-bottle-demo-abc12")


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

    def test_pipelock_path_when_no_routes(self):
        plan = _plan(pipelock_ca_path=self.pipelock_ca)
        with patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec"
        ) as ex:
            _ca.provision_ca(plan, "claude-bottle-demo-abc12")
        cp.assert_called_once_with(
            str(self.pipelock_ca),
            "claude-bottle-demo-abc12:" + _ca.AGENT_CA_PATH,
        )
        argvs = [c.args[1] for c in ex.call_args_list]
        self.assertIn(["chmod", "644", _ca.AGENT_CA_PATH], argvs)
        self.assertIn(["update-ca-certificates"], argvs)

    def test_egress_path_when_routes_declared(self):
        plan = _plan(
            egress_routes=(EgressRoute(host="api.anthropic.com"),),
            egress_ca_path=self.egress_ca,
            pipelock_ca_path=self.pipelock_ca,
        )
        with patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec"
        ):
            _ca.provision_ca(plan, "claude-bottle-demo-abc12")
        # When routes are declared, egress is the agent's first hop,
        # so egress's CA is the one that gets installed.
        cp.assert_called_once_with(
            str(self.egress_ca),
            "claude-bottle-demo-abc12:" + _ca.AGENT_CA_PATH,
        )

    def test_dies_when_selected_cert_missing(self):
        # Plan claims a pipelock cert at a path that doesn't exist —
        # something went wrong in launch's pipelock_tls_init.
        plan = _plan(pipelock_ca_path=self.tmp / "does-not-exist.pem")
        with patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_cp"
        ), patch(
            "claude_bottle.backend.smolmachines.provision.ca._smolvm.machine_exec"
        ):
            with self.assertRaises(SystemExit):
                _ca.provision_ca(plan, "claude-bottle-demo-abc12")


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
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git.provision_git(
                _plan(stage_dir=self.stage), "claude-bottle-demo-abc12",
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
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ) as ex:
            _git.provision_git(plan, "claude-bottle-demo-abc12")
        cp.assert_called_once_with(
            f"{cwd}/.git",
            "claude-bottle-demo-abc12:/home/node/workspace/.git",
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
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ):
            _git.provision_git(plan, "claude-bottle-demo-abc12")
        cp.assert_not_called()

    def test_writes_gitconfig_with_ip_port_form_for_smolmachines(self):
        # Smolmachines's TSI-allowlisted guest dials git-gate via
        # `127.0.0.1:<host port>` — the bundle's git-gate port is
        # published on host loopback at launch time, and the plan
        # carries the discovered host port (here mocked to 9418).
        plan = _plan(
            git=[GitEntry(
                Name="claude-bottle",
                Upstream="ssh://git@host/repo.git",
                IdentityFile="~/.ssh/id_ed25519",
            )],
            stage_dir=self.stage,
            agent_git_gate_host="127.0.0.1:9418",
        )
        with patch(
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_cp"
        ) as cp, patch(
            "claude_bottle.backend.smolmachines.provision.git._smolvm.machine_exec"
        ):
            _git.provision_git(plan, "claude-bottle-demo-abc12")
        # The staged gitconfig path is whatever NamedTemporaryFile
        # picked; we read its contents.
        cp_call = cp.call_args
        staged_path = Path(cp_call.args[0])
        self.assertEqual(self.stage, staged_path.parent)
        content = staged_path.read_text()
        self.assertIn(
            '[url "git://127.0.0.1:9418/claude-bottle.git"]', content,
        )
        self.assertIn(
            "\tinsteadOf = ssh://git@host/repo.git", content,
        )


class TestProvisionSupervise(unittest.TestCase):
    def test_noop_when_supervise_not_enabled(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec"
        ) as ex:
            _supervise.provision_supervise(_plan(), "claude-bottle-demo-abc12")
        ex.assert_not_called()

    def test_calls_claude_mcp_add_when_supervise_enabled(self):
        plan = _plan(
            supervise=True,
            agent_supervise_url="http://127.0.0.1:9100/",
        )
        with patch(
            "claude_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec",
            return_value=SmolvmRunResult(returncode=0, stdout="", stderr=""),
        ) as ex:
            _supervise.provision_supervise(plan, "claude-bottle-demo-abc12")
        ex.assert_called_once()
        argv = ex.call_args.args[1]
        # claude mcp add --scope user --transport http supervise <url>
        # — URL is the agent-side endpoint (host loopback +
        # discovered port), not the docker bridge IP.
        self.assertEqual(
            [
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
            "claude_bottle.backend.smolmachines.provision.supervise._smolvm.machine_exec",
            return_value=SmolvmRunResult(
                returncode=1, stdout="", stderr="boom",
            ),
        ):
            # No raise — the bottle still works without the MCP
            # entry, so we log and move on.
            _supervise.provision_supervise(plan, "claude-bottle-demo-abc12")


if __name__ == "__main__":
    unittest.main()
