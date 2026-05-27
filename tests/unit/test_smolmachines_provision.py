"""Unit: smolmachines provisioning helpers (PRD 0023 chunk 4a).

Tests mock `smolvm.machine_cp` / `smolvm.machine_exec` and assert
on the dispatched call shape. The real round-trip lives in the
chunk-4 integration smoke."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.backend import BottleSpec
from claude_bottle.backend.smolmachines.bottle_plan import (
    SmolmachinesBottlePlan,
)
from claude_bottle.backend.smolmachines.provision import (
    prompt as _prompt,
    skills as _skills,
)
from claude_bottle.egress import EgressPlan
from claude_bottle.git_gate import GitGatePlan
from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import PipelockProxyPlan


def _plan(
    *,
    agent_prompt: str = "",
    skills: list[str] | None = None,
) -> SmolmachinesBottlePlan:
    manifest = Manifest.from_json_obj({
        "bottles": {"dev": {}},
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
        copy_cwd=False,
        user_cwd="/tmp/x",
    )
    return SmolmachinesBottlePlan(
        spec=spec,
        stage_dir=Path("/tmp/stage"),
        slug="demo-abc12",
        bundle_subnet="192.168.50.0/24",
        bundle_gateway="192.168.50.1",
        bundle_ip="192.168.50.2",
        machine_name="claude-bottle-demo-abc12",
        agent_from_path=Path("/tmp/agent.smolmachine"),
        guest_env={},
        prompt_file=Path("/tmp/state/demo-abc12/agent/prompt.txt"),
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
    )


class TestProvisionPrompt(unittest.TestCase):
    def test_cp_uses_smolvm_machine_cp_with_machine_path_syntax(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp:
            _prompt.provision_prompt(_plan(), "claude-bottle-demo-abc12")
        cp.assert_called_once_with(
            "/tmp/state/demo-abc12/agent/prompt.txt",
            "claude-bottle-demo-abc12:/root/.claude-bottle-prompt.txt",
        )

    def test_returns_path_when_agent_has_prompt(self):
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ):
            r = _prompt.provision_prompt(
                _plan(agent_prompt="You are a helpful assistant."),
                "claude-bottle-demo-abc12",
            )
        self.assertEqual("/root/.claude-bottle-prompt.txt", r)

    def test_returns_none_when_agent_has_no_prompt(self):
        # The file is still copied (path-must-exist contract);
        # only the return value differs.
        with patch(
            "claude_bottle.backend.smolmachines.provision.prompt._smolvm.machine_cp"
        ) as cp:
            r = _prompt.provision_prompt(_plan(agent_prompt=""), "claude-bottle-demo-abc12")
        self.assertIsNone(r)
        cp.assert_called_once()


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

        # mkdir -p the skills dir once + rm -rf per skill = 3 exec calls.
        self.assertEqual(3, ex.call_count)
        mkdir_call = ex.call_args_list[0]
        self.assertEqual(
            ("claude-bottle-demo-abc12", ["mkdir", "-p", "/root/.claude/skills"]),
            mkdir_call.args,
        )
        # Two cp calls, one per skill, into the per-skill subdir.
        self.assertEqual(2, cp.call_count)
        cp_targets = {call.args[1] for call in cp.call_args_list}
        self.assertEqual(
            {
                "claude-bottle-demo-abc12:/root/.claude/skills/init-prd",
                "claude-bottle-demo-abc12:/root/.claude/skills/verify",
            },
            cp_targets,
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


if __name__ == "__main__":
    unittest.main()
