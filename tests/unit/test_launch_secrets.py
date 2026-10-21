"""Unit: launchers take secret values from the plan's `SecretSource` (PRD 0092).

Egress token_refs resolve from `plan.egress_secrets` (the provider's
provisioned values, then `plan.secrets`), never from this process's env;
firecracker's guest env takes forwarded values from the plan."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleImages, BottleSpec
from bot_bottle.backend.docker import launch as docker_launch
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.firecracker import launch as fc_launch
from bot_bottle.backend.firecracker.bottle_plan import FirecrackerBottlePlan
from bot_bottle.backend.macos_container import launch as mac_launch
from bot_bottle.backend.macos_container.bottle_plan import MacosContainerBottlePlan
from bot_bottle.contract.hostctl import Settings
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex
from bot_bottle.secret_source import MappingSecrets
from tests.unit import use_bottle_root

_INDEX = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
})
_SLUG = "demo-secrets1"
_SECRETS = MappingSecrets({"GH_PAT": "from-source", "HOST_CRED": "shadowed"})
_EXPECTED = {"EGRESS_TOKEN_0": "from-source", "EGRESS_TOKEN_1": "provisioned"}


def _same_plan(plan: MacosContainerBottlePlan, _endpoint: object) -> MacosContainerBottlePlan:
    return plan


class _Stop(Exception):
    """Ends a launch once the tokens have been handed over."""


def _fields(stage: Path) -> dict[str, Any]:
    return {
        "spec": BottleSpec(manifest=_INDEX, agent_name="demo", copy_cwd=False,
                           user_cwd=str(stage), identity=_SLUG),
        "manifest": _INDEX.load_for_agent("demo"),
        "stage_dir": stage,
        "git_gate_plan": GitGatePlan(
            slug=_SLUG, entrypoint_script=stage / "e.sh", hook_script=stage / "h.sh",
            access_hook_script=stage / "a.sh", upstreams=()),
        "egress_plan": EgressPlan(
            slug=_SLUG, routes_path=stage / "egress.yaml", routes=(),
            token_env_map={"EGRESS_TOKEN_0": "GH_PAT", "EGRESS_TOKEN_1": "HOST_CRED"}),
        "supervise_plan": None,
        "agent_provision": AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image="bot-bottle-claude:latest", dockerfile="", guest_home="/home/node",
            instance_name=f"bot-bottle-{_SLUG}", prompt_file=stage / "prompt.txt",
            guest_env={}, provisioned_env={"HOST_CRED": "provisioned"}),
        "settings": Settings(),
        "secrets": _SECRETS,
        "slug": _SLUG,
        "forwarded_env": {"FWD": "forwarded-value"},
    }


class _LaunchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stage = Path(tempfile.mkdtemp(prefix="launch-secrets."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))
        self.enterContext(
            mock.patch.dict(os.environ, {"GH_PAT": "from-env", "HOST_CRED": "from-env"}))

    def _tokens(self, register: mock.Mock) -> dict[str, str]:
        return register.call_args.kwargs["tokens"]


class TestDockerLaunchSecrets(_LaunchCase):
    def test_token_refs_resolve_from_the_plan(self) -> None:
        plan = DockerBottlePlan(**_fields(self.stage), use_runsc=False)
        images = BottleImages(agent="a:latest", sidecar="s:latest")
        with mock.patch.object(docker_launch, "launch_consolidated",
                               side_effect=_Stop) as register, \
                mock.patch.object(docker_launch, "resolve_teardown_timeout", return_value=1), \
                self.assertRaises(_Stop):
            with docker_launch.launch(plan, images, provision=mock.Mock()):
                pass
        self.assertEqual(_EXPECTED, self._tokens(register))


class TestFirecrackerLaunchSecrets(_LaunchCase):
    def test_token_refs_resolve_from_the_plan(self) -> None:
        plan = FirecrackerBottlePlan(**_fields(self.stage))
        with mock.patch.object(fc_launch, "netpool") as netpool, \
                mock.patch.object(fc_launch, "info"), \
                mock.patch.object(fc_launch, "launch_consolidated",
                                  side_effect=_Stop) as register, \
                mock.patch.object(fc_launch, "resolve_teardown_timeout", return_value=1), \
                self.assertRaises(_Stop):
            netpool.from_settings.return_value.allocate.return_value = (
                mock.Mock(iface="t0", host_ip="10.0.0.0", guest_ip="10.0.0.1"), mock.Mock())
            with fc_launch.launch(plan, self.stage / "base", provision=mock.Mock()):
                pass
        self.assertEqual(_EXPECTED, self._tokens(register))

    def test_guest_env_takes_forwarded_values_from_the_plan(self) -> None:
        plan = FirecrackerBottlePlan(**_fields(self.stage))
        with mock.patch.dict(os.environ, {"FWD": "from-env"}):
            env = fc_launch._agent_guest_env(plan, "10.0.0.0")  # pylint: disable=protected-access
        self.assertEqual("forwarded-value", env["FWD"])


class TestMacosLaunchSecrets(_LaunchCase):
    def test_token_refs_resolve_from_the_plan(self) -> None:
        plan = MacosContainerBottlePlan(**_fields(self.stage))
        images = BottleImages(agent="a:latest", sidecar="s:latest")
        with mock.patch.object(mac_launch, "MacosInfraService"), \
                mock.patch.object(mac_launch, "ensure_gateway"), \
                mock.patch.object(mac_launch, "_install_gateway_ca",
                                  side_effect=_same_plan), \
                mock.patch.object(mac_launch, "_stamp_agent_urls",
                                  side_effect=_same_plan), \
                mock.patch.object(mac_launch, "new_env_var_secret", return_value="k"), \
                mock.patch.object(mac_launch, "container_mod") as container_mod, \
                mock.patch.object(mac_launch, "_start_agent"), \
                mock.patch.object(mac_launch, "set_gateway_host"), \
                mock.patch.object(mac_launch, "resolve_teardown_timeout", return_value=1), \
                mock.patch.object(mac_launch, "register_agent",
                                  side_effect=_Stop) as register, \
                self.assertRaises(_Stop):
            container_mod.wait_container_ipv4_on_network.return_value = "192.168.64.5"
            with mac_launch.launch(plan, images, provision=mock.Mock()):
                pass
        self.assertEqual(_EXPECTED, self._tokens(register))


class TestEgressSecrets(unittest.TestCase):
    def test_provisioned_values_win_then_the_plan_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = FirecrackerBottlePlan(**_fields(Path(tmp)))
        self.assertEqual("provisioned", plan.egress_secrets.get("HOST_CRED"))
        self.assertEqual("from-source", plan.egress_secrets.get("GH_PAT"))
        self.assertIsNone(plan.egress_secrets.get("UNSET"))

    def test_plan_repr_hides_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = FirecrackerBottlePlan(**_fields(Path(tmp)))
        self.assertNotIn("from-source", repr(plan))


if __name__ == "__main__":
    unittest.main()
