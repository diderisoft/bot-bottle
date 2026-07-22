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
from bot_bottle.backend import BottleImages, BottleSpec
from bot_bottle.backend.docker import launch as launch_mod
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.docker.consolidated_launch import LaunchContext
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex
from tests.unit import use_bottle_root

_INDEX = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
})


def _plan(tmp: str) -> DockerBottlePlan:
    stage = Path(tmp)
    manifest = _INDEX.load_for_agent("demo")
    spec = BottleSpec(
        manifest=_INDEX,
        agent_name="demo",
        copy_cwd=False,
        user_cwd=tmp,
        identity="test-teardown-00001",
    )
    return DockerBottlePlan(
        spec=spec,
        manifest=manifest,
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
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_home="/home/node",
            instance_name="bot-bottle-test-teardown-abc",
            prompt_file=stage / "prompt.txt",
            guest_env={},
        ),
        slug="test-teardown-00001",
        forwarded_env={},
        use_runsc=False,
    )


class TestTeardownWarning(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="docker-launch-teardown-test.")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(use_bottle_root(Path(self._tmp)))  # sandbox the gateway CA write

    def test_teardown_failure_emits_warning_with_container_and_operation(self):
        plan = _plan(self._tmp)
        buf = io.StringIO()
        gw = mock.Mock()
        gw.ca_cert_pem.return_value = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        ctx = LaunchContext(
            bottle_id="b1", identity_token="t", source_ip="172.20.0.4",
            network="bot-bottle-gateway", gateway_ip="172.20.0.2",
            orchestrator_url="http://orch:8099",
            env_var_secret="encryption-key",
        )

        images = BottleImages(agent="bot-bottle-claude:latest", sidecar="bot-bottle-sidecars:latest")

        with mock.patch.object(launch_mod.docker_mod, "build_image"), \
             mock.patch.object(launch_mod.docker_mod, "verify_agent_image"), \
             mock.patch.object(launch_mod, "launch_consolidated", return_value=ctx), \
             mock.patch.object(launch_mod, "teardown_consolidated"), \
             mock.patch.object(launch_mod, "DockerGateway", return_value=gw), \
             mock.patch.object(
                 launch_mod, "consolidated_agent_compose",
                 return_value={"services": {"agent": {}}},
             ), \
             mock.patch.object(
                 launch_mod, "write_compose_file", return_value=Path("/tmp/compose.yml"),
             ), \
             mock.patch.object(launch_mod, "compose_up") as compose_up, \
             mock.patch.object(launch_mod, "compose_dump_logs"), \
             mock.patch.object(
                 launch_mod, "compose_down",
                 side_effect=RuntimeError("compose down failed"),
             ), \
             contextlib.redirect_stderr(buf):
            provision = mock.Mock(return_value=None)
            with launch_mod.launch(plan, images, provision=provision):
                pass

        output = buf.getvalue()
        self.assertIn("bot-bottle: warning:", output)
        self.assertIn("bot-bottle-test-teardown-abc", output)
        self.assertIn("compose-down", output)
        self.assertEqual(
            "encryption-key", compose_up.call_args.kwargs["env"]["ENV_VAR_SECRET"],
        )


if __name__ == "__main__":
    unittest.main()
