"""Unit: Docker launch step uses committed image when available (consolidated)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import tempfile
import unittest
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker import launch as launch_mod
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.docker.consolidated_launch import LaunchContext
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestIndex
from tests.unit import use_bottle_root


_SLUG = "dev-abc12"
_COMMITTED_TAG = f"bot-bottle-committed-{_SLUG}:latest"
_DEFAULT_IMAGE = "bot-bottle-claude:latest"

_IDX = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
})

_CTX = LaunchContext(
    bottle_id="b1", identity_token="t", source_ip="172.20.0.4",
    network="bot-bottle-gateway", gateway_ip="172.20.0.2",
)


def _plan(tmp: str) -> DockerBottlePlan:
    stage = Path(tmp)
    spec = BottleSpec(
        manifest=_IDX, agent_name="demo", copy_cwd=False, user_cwd=tmp, identity=_SLUG,
    )
    return DockerBottlePlan(
        spec=spec,
        manifest=_IDX.load_for_agent("demo"),
        stage_dir=stage,
        git_gate_plan=GitGatePlan(
            slug=_SLUG, entrypoint_script=stage / "e.sh", hook_script=stage / "h.sh",
            access_hook_script=stage / "a.sh", upstreams=(),
        ),
        egress_plan=EgressPlan(
            slug=_SLUG, routes_path=stage / "egress.yaml", routes=(), token_env_map={},
        ),
        supervise_plan=None,
        agent_provision=AgentProvisionPlan(
            template="claude", command="claude", prompt_mode="append_file",
            image=_DEFAULT_IMAGE, dockerfile="", guest_home="/home/node",
            instance_name=f"bot-bottle-{_SLUG}", prompt_file=stage / "prompt.txt",
            guest_env={},
        ),
        slug=_SLUG, forwarded_env={}, use_runsc=False,
        settings=Settings(),
        secrets=MappingSecrets({}),
    )


class TestLaunchCommittedImage(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="launch-committed-test.")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        # The launch writes the gateway CA under the bottle root — sandbox it.
        self.addCleanup(use_bottle_root(Path(self._tmp)))

    @contextlib.contextmanager
    def _patched(
        self,
        *,
        committed_tag: str | None,
        image_present: bool,
        compose: Callable[..., dict[str, Any]],
    ) -> Generator[list[str], None, None]:
        built: list[str] = []
        gw = mock.Mock()
        gw.ca_cert_pem.return_value = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"

        def _build(image: str, ctx: str, *, dockerfile: str = "", no_cache: bool = False) -> None:
            del ctx, dockerfile, no_cache
            built.append(image)

        with mock.patch.object(launch_mod, "read_committed_image", return_value=committed_tag), \
                mock.patch.object(launch_mod.docker_mod, "image_exists", return_value=image_present), \
                mock.patch.object(launch_mod.docker_mod, "build_image", side_effect=_build), \
                mock.patch.object(launch_mod.docker_mod, "verify_agent_image"), \
                mock.patch.object(launch_mod.docker_mod, "image_created_at"), \
                mock.patch.object(launch_mod, "launch_consolidated", return_value=_CTX), \
                mock.patch.object(launch_mod, "deprovision_consolidated"), \
                mock.patch.object(launch_mod, "DockerGateway", return_value=gw), \
                mock.patch.object(launch_mod, "consolidated_agent_compose", side_effect=compose), \
                mock.patch.object(launch_mod, "write_compose_file", return_value=Path("/tmp/c.yml")), \
                mock.patch.object(launch_mod, "compose_up"), \
                mock.patch.object(launch_mod, "compose_dump_logs"), \
                mock.patch.object(launch_mod, "compose_down"), \
                mock.patch.object(launch_mod.isolation_probe, "verify_isolation"), \
                contextlib.redirect_stderr(io.StringIO()):
            yield built

    def _run_launch(
        self, plan: DockerBottlePlan, *,
        committed_tag: str | None = None, image_present: bool = True,
    ) -> list[str]:
        def compose(_p: DockerBottlePlan, **_kw: Any) -> dict[str, Any]:
            return {"services": {"agent": {}}}
        with self._patched(
            committed_tag=committed_tag, image_present=image_present, compose=compose,
        ) as built:
            images = launch_mod.build_or_load_images(plan)
            with launch_mod.launch(plan, images, provision=mock.Mock(return_value=None)):
                pass
        return built

    def test_skips_build_when_committed_image_present(self) -> None:
        built = self._run_launch(_plan(self._tmp), committed_tag=_COMMITTED_TAG, image_present=True)
        self.assertEqual([], built)  # committed image reused, no build

    def test_uses_committed_image_in_compose_spec(self) -> None:
        captured: list[DockerBottlePlan] = []

        def compose(p: DockerBottlePlan, **_kw: Any) -> dict[str, Any]:
            captured.append(p)
            return {"services": {"agent": {}}}

        with self._patched(committed_tag=_COMMITTED_TAG, image_present=True, compose=compose) as _:
            plan = _plan(self._tmp)
            images = launch_mod.build_or_load_images(plan)
            with launch_mod.launch(plan, images, provision=mock.Mock(return_value=None)):
                pass
        self.assertEqual(_COMMITTED_TAG, captured[0].image)

    def test_falls_back_to_build_when_no_committed_image(self) -> None:
        self.assertEqual([_DEFAULT_IMAGE], self._run_launch(_plan(self._tmp), committed_tag=None))

    def test_runs_the_published_image_when_one_resolves(self) -> None:
        plan = _plan(self._tmp)
        with mock.patch.object(launch_mod, "read_committed_image", return_value=None), \
                mock.patch.object(launch_mod.oci_agent_image, "resolve_published_agent_image",
                                  return_value="bot-bottle-claude:pin-abc") as resolve, \
                mock.patch.object(launch_mod.docker_mod, "build_image") as build:
            images = launch_mod.build_or_load_images(plan)
        self.assertEqual("bot-bottle-claude:pin-abc", images.agent)
        self.assertEqual(("docker", "pull"), resolve.call_args.args[1].pull_argv)
        build.assert_not_called()

    def test_builds_its_own_dockerfile_when_nothing_is_published(self) -> None:
        plan = _plan(self._tmp)
        with mock.patch.object(launch_mod, "read_committed_image", return_value=None), \
                mock.patch.object(launch_mod.oci_agent_image, "resolve_published_agent_image",
                                  return_value=None), \
                mock.patch.object(launch_mod.docker_mod, "verify_agent_image"), \
                mock.patch.object(launch_mod.docker_mod, "build_image") as build:
            images = launch_mod.build_or_load_images(plan)
        build.assert_called_once()
        self.assertEqual(plan.image, images.agent)

    def test_cached_images_skip_build_when_present(self) -> None:
        base = _plan(self._tmp)
        plan = dataclasses.replace(
            base,
            spec=dataclasses.replace(base.spec, image_policy="cached"),
        )
        built = self._run_launch(plan, committed_tag=None, image_present=True)
        self.assertEqual([], built)

    def test_cached_images_die_when_agent_missing(self) -> None:
        base = _plan(self._tmp)
        plan = dataclasses.replace(
            base,
            spec=dataclasses.replace(base.spec, image_policy="cached"),
        )
        with self.assertRaises(Die):
            self._run_launch(plan, committed_tag=None, image_present=False)

    def test_prefer_cached_skips_build_when_present_and_builds_when_missing(self) -> None:
        base = _plan(self._tmp)
        plan = dataclasses.replace(
            base, spec=dataclasses.replace(base.spec, image_policy="prefer-cached"))
        self.assertEqual([], self._run_launch(plan, committed_tag=None, image_present=True))
        self.assertEqual([_DEFAULT_IMAGE],
                         self._run_launch(plan, committed_tag=None, image_present=False))

    def test_falls_back_to_build_when_committed_image_missing_from_daemon(self) -> None:
        built = self._run_launch(_plan(self._tmp), committed_tag=_COMMITTED_TAG, image_present=False)
        self.assertEqual([_DEFAULT_IMAGE], built)


if __name__ == "__main__":
    unittest.main()
