"""Unit: BottlePlan.print parity across Docker and smolmachines (PRD 0044).

Both backends inherit a single concrete print() from BottlePlan. These
tests verify that identical git_gate_plan and egress_plan inputs produce
identical preflight output regardless of backend-specific fields.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.smolmachines.bottle_plan import SmolmachinesBottlePlan
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream
from bot_bottle.manifest import Manifest, ManifestIndex


_INDEX = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
})


def _spec(index: ManifestIndex, tmp: str) -> BottleSpec:
    return BottleSpec(
        manifest=index,
        agent_name="demo",
        copy_cwd=False,
        user_cwd=tmp,
        identity="test-00001",
    )


def _git_gate_plan(tmp: str) -> GitGatePlan:
    stage = Path(tmp)
    return GitGatePlan(
        slug="test-00001",
        entrypoint_script=stage / "entrypoint.sh",
        hook_script=stage / "hook.sh",
        access_hook_script=stage / "access-hook.sh",
        upstreams=(
            GitGateUpstream(
                name="myrepo",
                upstream_url="ssh://git@gitea.example.com:30009/org/myrepo.git",
                upstream_host="gitea.example.com",
                upstream_port="30009",
                identity_file="/dev/null",
                known_host_key="ssh-ed25519 AAAA...",
            ),
        ),
    )


def _egress_plan(tmp: str) -> EgressPlan:
    return EgressPlan(
        slug="test-00001",
        routes_path=Path(tmp) / "egress.yaml",
        routes=(
            EgressRoute(
                host="api.example.com",
                auth_scheme="bearer",
                token_env="EGRESS_TOKEN_0",
                token_ref="TOKEN",
            ),
            EgressRoute(
                host="static.example.com",
            ),
        ),
        token_env_map={"EGRESS_TOKEN_0": "TOKEN"},
    )


def _agent_provision(tmp: str) -> AgentProvisionPlan:
    return AgentProvisionPlan(
        template="claude",
        command="claude",
        prompt_mode="append_file",
        image="bot-bottle-claude:latest",
        dockerfile="",
        guest_home="/home/node",
        instance_name="bot-bottle-test-00001",
        prompt_file=Path(tmp) / "prompt.txt",
        guest_env={"HTTPS_PROXY": "http://127.0.0.1:9999"},
    )


def _docker_plan(spec: BottleSpec, manifest: Manifest, tmp: str) -> DockerBottlePlan:
    stage = Path(tmp)
    return DockerBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=stage,
        git_gate_plan=_git_gate_plan(tmp),
        egress_plan=_egress_plan(tmp),
        supervise_plan=None,
        agent_provision=_agent_provision(tmp),
        slug="test-00001",
        forwarded_env={},
        use_runsc=False,
    )


def _smolmachines_plan(spec: BottleSpec, manifest: Manifest, tmp: str) -> SmolmachinesBottlePlan:
    stage = Path(tmp)
    return SmolmachinesBottlePlan(
        spec=spec,
        manifest=manifest,
        stage_dir=stage,
        git_gate_plan=_git_gate_plan(tmp),
        egress_plan=_egress_plan(tmp),
        supervise_plan=None,
        agent_provision=_agent_provision(tmp),
        slug="test-00001",
        bundle_subnet="10.99.0.0/24",
        bundle_gateway="10.99.0.1",
        bundle_ip="10.99.0.2",
        guest_env={"HTTPS_PROXY": "http://127.0.0.1:9999"},
    )


def _capture_print(plan: DockerBottlePlan | SmolmachinesBottlePlan) -> list[str]:
    buf = io.StringIO()
    orig = sys.stderr
    sys.stderr = buf
    try:
        plan.print()
    finally:
        sys.stderr = orig
    return buf.getvalue().splitlines()


class TestGitGatePrintParity(unittest.TestCase):
    """Both backends render git gate entries as 'name → host:port'."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="plan-print-parity-")
        manifest = _INDEX.load_for_agent("demo")
        spec = _spec(_INDEX, self._tmp)
        self._docker_lines = _capture_print(_docker_plan(spec, manifest, self._tmp))
        self._smol_lines = _capture_print(_smolmachines_plan(spec, manifest, self._tmp))

    def _git_gate_lines(self, lines: list[str]) -> list[str]:
        return [ln for ln in lines if "git gate" in ln]

    def test_docker_renders_name_arrow_host_port(self) -> None:
        git_lines = self._git_gate_lines(self._docker_lines)
        self.assertEqual(1, len(git_lines))
        self.assertIn("myrepo → gitea.example.com:30009", git_lines[0])

    def test_smolmachines_renders_name_arrow_host_port(self) -> None:
        git_lines = self._git_gate_lines(self._smol_lines)
        self.assertEqual(1, len(git_lines))
        self.assertIn("myrepo → gitea.example.com:30009", git_lines[0])

    def test_git_gate_lines_match_across_backends(self) -> None:
        self.assertEqual(
            self._git_gate_lines(self._docker_lines),
            self._git_gate_lines(self._smol_lines),
        )


class TestEgressPrintParity(unittest.TestCase):
    """Both backends render egress with auth annotation where present."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="plan-print-parity-")
        manifest = _INDEX.load_for_agent("demo")
        spec = _spec(_INDEX, self._tmp)
        self._docker_lines = _capture_print(_docker_plan(spec, manifest, self._tmp))
        self._smol_lines = _capture_print(_smolmachines_plan(spec, manifest, self._tmp))

    def _egress_section(self, lines: list[str]) -> list[str]:
        """Return lines from the egress label through the last route entry.

        print_multi renders the first route on the label line and
        aligns additional routes as indented continuation lines
        (no repeated label). Collect the label line plus every
        non-blank, non-labelled line that follows before the next
        top-level section begins."""
        result: list[str] = []
        collecting = False
        indent_prefix = None
        for ln in lines:
            stripped = ln.lstrip()
            if "egress" in stripped and ":" in stripped:
                collecting = True
                # Determine the continuation indent from this line's prefix.
                idx = ln.index("egress")
                indent_prefix = ln[:idx]
                result.append(ln)
            elif collecting:
                if (
                    ln.startswith(indent_prefix)  # type: ignore
                    and "egress" not in ln
                    and ":" not in ln.lstrip()[:20]
                ):
                    result.append(ln)
                else:
                    break
        return result

    def test_docker_includes_auth_annotation(self) -> None:
        combined = "\n".join(self._egress_section(self._docker_lines))
        self.assertIn("api.example.com [auth:bearer]", combined)

    def test_smolmachines_includes_auth_annotation(self) -> None:
        combined = "\n".join(self._egress_section(self._smol_lines))
        self.assertIn("api.example.com [auth:bearer]", combined)

    def test_unauthenticated_route_has_no_annotation(self) -> None:
        full = "\n".join(self._docker_lines)
        self.assertIn("static.example.com", full)
        self.assertNotIn("static.example.com [auth:", full)

    def test_egress_lines_match_across_backends(self) -> None:
        self.assertEqual(
            self._egress_section(self._docker_lines),
            self._egress_section(self._smol_lines),
        )


if __name__ == "__main__":
    unittest.main()
