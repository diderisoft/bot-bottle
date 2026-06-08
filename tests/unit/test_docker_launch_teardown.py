"""Unit: Docker launch teardown warning on ExitStack failure (issue #156).

When a callback registered in the ExitStack raises during teardown,
the teardown function must emit a WARNING-level message that includes
the container name and operation type, rather than silently discarding
the exception.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker import launch as launch_mod
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest
# from bot_bottle.workspace import workspace_plan


def _manifest() -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })


def _plan(tmp: str) -> DockerBottlePlan:
    stage = Path(tmp)
    manifest = _manifest()
    spec = BottleSpec(
        manifest=manifest,
        agent_name="demo",
        copy_cwd=False,
        user_cwd=tmp,
        identity="test-teardown-00001",
    )
    return DockerBottlePlan(
        spec=spec,
        stage_dir=stage,
        git_gate_plan=GitGatePlan(
            slug="test-teardown-00001",
            entrypoint_script=stage / "entrypoint.sh",
            hook_script=stage / "hook.sh",
            access_hook_script=stage / "access-hook.sh",
            upstreams=(),
        ),
        egress_plan=EgressPlan(
            slug="test-teardown-00001",
            routes_path=stage / "egress.yaml",
            routes=(),
            token_env_map={},
        ),
        supervise_plan=None,
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="",
            dockerfile="",
            guest_home="/home/node",
            guest_env={},
        ),
        slug="test-teardown-00001",
        container_name="bot-bottle-test-teardown-abc",
        image="bot-bottle-claude:latest",
        dockerfile_path="",
        forwarded_env={},
        prompt_file=stage / "prompt.txt",
        use_runsc=False,
    )


class TestTeardownWarning(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="docker-launch-teardown-test.")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_teardown_failure_emits_warning_with_container_and_operation(self):
        plan = _plan(self._tmp)
        buf = io.StringIO()

        with mock.patch.object(launch_mod.docker_mod, "build_image"), \
             mock.patch.object(
                 launch_mod, "egress_tls_init",
                 return_value=(Path("/egress_ca"), Path("/egress_cert")),
             ), \
             mock.patch.object(
                 launch_mod.network_mod, "network_name_for_slug",
                 return_value="bb-internal-test",
             ), \
             mock.patch.object(
                 launch_mod.network_mod, "network_egress_name_for_slug",
                 return_value="bb-egress-test",
             ), \
             mock.patch.object(
                 launch_mod, "bottle_plan_to_compose",
                 return_value={"services": {"agent": {}}},
             ), \
             mock.patch.object(
                 launch_mod, "write_compose_file",
                 return_value=Path("/tmp/compose.yml"),
             ), \
             mock.patch.object(launch_mod, "compose_up"), \
             mock.patch.object(launch_mod, "compose_dump_logs"), \
             mock.patch.object(
                 launch_mod, "compose_down",
                 side_effect=RuntimeError("network remove failed"),
             ), \
             contextlib.redirect_stderr(buf):
            provision = mock.Mock(return_value=None)
            with launch_mod.launch(plan, provision=provision):
                pass

        output = buf.getvalue()
        self.assertIn("bot-bottle: warning:", output)
        self.assertIn("bot-bottle-test-teardown-abc", output)
        self.assertIn("compose-down", output)


if __name__ == "__main__":
    unittest.main()
