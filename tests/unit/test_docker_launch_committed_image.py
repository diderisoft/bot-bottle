"""Unit: Docker launch step uses committed image when available."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker import launch as launch_mod
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import Manifest


_SLUG = "dev-abc12"
_COMMITTED_TAG = f"bot-bottle-committed-{_SLUG}:latest"
_DEFAULT_IMAGE = "bot-bottle-claude:latest"


def _manifest() -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })


def _plan(tmp: str) -> DockerBottlePlan:
    stage = Path(tmp)
    spec = BottleSpec(
        manifest=_manifest(),
        agent_name="demo",
        copy_cwd=False,
        user_cwd=tmp,
        identity=_SLUG,
    )
    return DockerBottlePlan(
        spec=spec,
        stage_dir=stage,
        git_gate_plan=GitGatePlan(
            slug=_SLUG,
            entrypoint_script=stage / "entrypoint.sh",
            hook_script=stage / "hook.sh",
            access_hook_script=stage / "access-hook.sh",
            upstreams=(),
        ),
        egress_plan=EgressPlan(
            slug=_SLUG,
            routes_path=stage / "egress.yaml",
            routes=(),
            token_env_map={},
        ),
        supervise_plan=None,
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image=_DEFAULT_IMAGE,
            dockerfile="",
            guest_home="/home/node",
            instance_name=f"bot-bottle-{_SLUG}",
            prompt_file=stage / "prompt.txt",
            guest_env={},
        ),
        slug=_SLUG,
        forwarded_env={},
        use_runsc=False,
    )


class TestLaunchCommittedImage(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="launch-committed-test.")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_launch(
        self,
        plan: DockerBottlePlan,
        *,
        committed_tag: str | None = None,
        image_present: bool = True,
    ) -> list[str]:
        """Drive launch() through its full sequence with the committed-image
        behaviour controlled by the arguments. Returns the images that were
        passed to `build_image` (empty list if it was never called)."""
        built: list[str] = []

        def fake_build(image: str, ctx: str, *, dockerfile: str = "") -> None:
            del ctx, dockerfile
            built.append(image)

        with mock.patch.object(
            launch_mod, "read_committed_image", return_value=committed_tag,
        ), mock.patch.object(
            launch_mod.docker_mod, "image_exists", return_value=image_present,
        ), mock.patch.object(
            launch_mod.docker_mod, "build_image", side_effect=fake_build,
        ), mock.patch.object(
            launch_mod, "egress_tls_init",
            return_value=(Path("/egress_ca"), Path("/egress_cert")),
        ), mock.patch.object(
            launch_mod.network_mod, "network_name_for_slug",
            return_value="bb-internal",
        ), mock.patch.object(
            launch_mod.network_mod, "network_egress_name_for_slug",
            return_value="bb-egress",
        ), mock.patch.object(
            launch_mod, "bottle_plan_to_compose",
            return_value={"services": {"agent": {}}},
        ), mock.patch.object(
            launch_mod, "write_compose_file",
            return_value=Path("/tmp/compose.yml"),
        ), mock.patch.object(launch_mod, "compose_up"), \
           mock.patch.object(launch_mod, "compose_dump_logs"), \
           mock.patch.object(launch_mod, "compose_down"), \
           contextlib.redirect_stderr(io.StringIO()):
            provision = mock.Mock(return_value=None)
            with launch_mod.launch(plan, provision=provision):
                pass

        return built

    def test_skips_build_when_committed_image_present(self) -> None:
        plan = _plan(self._tmp)
        built = self._run_launch(plan, committed_tag=_COMMITTED_TAG, image_present=True)
        self.assertEqual([], built, "build_image should not be called when committed image exists")

    def test_uses_committed_image_in_compose_spec(self) -> None:
        """The compose spec renderer receives the committed image tag via
        plan.image — captured here by checking what bottle_plan_to_compose
        was called with."""
        plan = _plan(self._tmp)
        captured_plans: list[DockerBottlePlan] = []

        def fake_compose(p: DockerBottlePlan) -> dict[str, Any]:
            captured_plans.append(p)
            return {"services": {"agent": {}}}

        with mock.patch.object(
            launch_mod, "read_committed_image", return_value=_COMMITTED_TAG,
        ), mock.patch.object(
            launch_mod.docker_mod, "image_exists", return_value=True,
        ), mock.patch.object(
            launch_mod.docker_mod, "build_image",
        ), mock.patch.object(
            launch_mod, "egress_tls_init",
            return_value=(Path("/egress_ca"), Path("/egress_cert")),
        ), mock.patch.object(
            launch_mod.network_mod, "network_name_for_slug",
            return_value="bb-internal",
        ), mock.patch.object(
            launch_mod.network_mod, "network_egress_name_for_slug",
            return_value="bb-egress",
        ), mock.patch.object(
            launch_mod, "bottle_plan_to_compose", side_effect=fake_compose,
        ), mock.patch.object(
            launch_mod, "write_compose_file",
            return_value=Path("/tmp/compose.yml"),
        ), mock.patch.object(launch_mod, "compose_up"), \
           mock.patch.object(launch_mod, "compose_dump_logs"), \
           mock.patch.object(launch_mod, "compose_down"), \
           contextlib.redirect_stderr(io.StringIO()):
            provision = mock.Mock(return_value=None)
            with launch_mod.launch(plan, provision=provision):
                pass

        self.assertEqual(1, len(captured_plans))
        self.assertEqual(_COMMITTED_TAG, captured_plans[0].image)

    def test_falls_back_to_build_when_no_committed_image(self) -> None:
        plan = _plan(self._tmp)
        built = self._run_launch(plan, committed_tag=None)
        self.assertEqual([_DEFAULT_IMAGE], built)

    def test_falls_back_to_build_when_committed_image_missing_from_daemon(self) -> None:
        plan = _plan(self._tmp)
        built = self._run_launch(
            plan, committed_tag=_COMMITTED_TAG, image_present=False,
        )
        self.assertEqual([_DEFAULT_IMAGE], built)


if __name__ == "__main__":
    unittest.main()
