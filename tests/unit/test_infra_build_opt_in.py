"""Unit: building the infra images is opt-in, never a fallback.

A release whose published images match pulls them; an install with nothing to
match stops before bring-up touches anything, unless `bot-bottle up --build`
(or `BOT_BOTTLE_INFRA_BUILD=local`) asked for a local build."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.backend.gateway_attach import AttachPolicy
from bot_bottle.backend import infra_service as infra_service_mod
from bot_bottle.backend.firecracker import infra_rootfs, infra_vm
from bot_bottle.backend.firecracker.infra import FirecrackerInfraService
from bot_bottle.backend.infra_service import InfraService, infra_settings_record
from bot_bottle.backend.macos_container.infra import MacosInfraService
from bot_bottle.contract.hostctl import InfraSettings, Settings
from bot_bottle.control.client import (
    OrchestratorClient, OrchestratorEndpoint, recorded_orchestrator,
)
from bot_bottle.infra_image import BUILD_NEEDS_DOCKER, BUILD_WITH_CONTAINER
from bot_bottle.lib.log import Die
from bot_bottle.published_artifacts import FORMAT_EXT4, FORMAT_OCI

_LOCAL = Settings(infra=InfraSettings(infra_build_local=True))


class _Service(InfraService):
    """An infra pair whose planes are mocks; records whether it was touched."""

    name = "fake"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.brought_up_with: list[Settings] = []
        self.stopped = False

    def orchestrator(self):  # type: ignore[no-untyped-def]
        return MagicMock()

    def gateway(self):  # type: ignore[no-untyped-def]
        return MagicMock()

    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        self.brought_up_with.append(self.settings)
        return "http://orch", True

    def stop(self) -> None:
        self.stopped = True

    def gateway_attacher(self):  # type: ignore[no-untyped-def]
        return MagicMock()


class _BringUp(unittest.TestCase):
    """A throwaway state root, a matching release and no reattaching."""

    root: Path

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target in ("bot_bottle.backend.infra_service.bot_bottle_root",
                       "bot_bottle.paths.bot_bottle_root"):
            p = patch(target, return_value=self.root)
            p.start()
            self.addCleanup(p.stop)
        p = patch("bot_bottle.infra_image.install_version", return_value=("0.1.4", "abcdef012345"))
        p.start()
        self.addCleanup(p.stop)
        # Reattaching is covered by the shared harness's tests.
        p = patch.object(infra_service_mod, "reattach_running_bottles")
        p.start()
        self.addCleanup(p.stop)

    def _matches(self, value: bool):  # type: ignore[no-untyped-def]
        return patch.object(infra_service_mod, "published_infra_matches", return_value=value)

    def _record(self, infra: InfraSettings) -> None:
        path = infra_settings_record("fake")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fingerprint": infra.fingerprint(),
                                    "infra": Settings(infra=infra).to_json()["infra"]}))

    def _ensure(self, svc: _Service, *, inherit_build: bool = True) -> str:
        with redirect_stderr(io.StringIO()):
            return svc.ensure_running(inherit_build=inherit_build)


class TestBringUpDecision(_BringUp):
    def test_a_matching_release_pulls(self) -> None:
        svc = _Service(Settings())
        with self._matches(True):
            self.assertEqual("http://orch", self._ensure(svc))
        self.assertFalse(svc.brought_up_with[0].infra.infra_build_local)

    def test_no_match_without_opt_in_stops_before_touching_anything(self) -> None:
        svc = _Service(Settings())
        # A healthy pair on other settings would otherwise be stopped first.
        self._record(InfraSettings(egress_port=1))
        with self._matches(False), patch.object(svc, "is_healthy", return_value=True), \
                self.assertRaises(Die) as caught:
            self._ensure(svc)
        self.assertEqual([], svc.brought_up_with)
        self.assertFalse(svc.stopped)
        self.assertEqual(
            "this install (0.1.4 @ abcdef012345, unreleased) has no published infra images.\n"
            "    Install a release: curl -fsSL \"https://gitea.dideric.is/api/v1/repos/"
            "didericis/bot-bottle/raw/install.sh?ref=main\" | sh\n"
            "    Or build them locally (needs Docker): bot-bottle up --build\n"
            "    (BOT_BOTTLE_INFRA_BUILD=local does the same for every command.)",
            caught.exception.message)

    def test_no_match_with_opt_in_builds(self) -> None:
        svc = _Service(_LOCAL)
        with self._matches(False):
            self._ensure(svc)
        self.assertTrue(svc.brought_up_with[0].infra.infra_build_local)

    def test_a_launch_keeps_the_last_ups_build_opt_in(self) -> None:
        # `up --build`, then `bot-bottle <agent>` without the env var: still
        # builds, on the same fingerprint (so the pair isn't restarted).
        self._record(_LOCAL.infra)
        svc = _Service(Settings())
        with self._matches(False):
            self._ensure(svc)
        self.assertTrue(svc.brought_up_with[0].infra.infra_build_local)
        self.assertEqual(svc.recorded_infra_fingerprint(), svc.settings.infra.fingerprint())

    def test_up_does_only_what_its_flags_say(self) -> None:
        # Cold: nothing running to keep, so an `up` without `--build` needs a
        # release to pull.
        self._record(_LOCAL.infra)
        svc = _Service(Settings())
        with self._matches(False), patch.object(svc, "is_healthy", return_value=False), \
                self.assertRaises(Die):
            self._ensure(svc, inherit_build=False)
        self.assertEqual([], svc.brought_up_with)

    def test_up_keeps_a_running_current_local_build(self) -> None:
        # `up --build`, then `up --restart`: the pair is running and current,
        # nothing needs pulling or building, so it's kept, opt-in and all,
        # on the same fingerprint (so it isn't stopped).
        self._record(_LOCAL.infra)
        svc = _Service(Settings())
        err = io.StringIO()
        with self._matches(False), patch.object(svc, "is_healthy", return_value=True), \
                redirect_stderr(err):
            self.assertEqual("http://orch", svc.ensure_running(inherit_build=False))
        self.assertFalse(svc.stopped)
        self.assertTrue(svc.settings.infra.infra_build_local)
        self.assertEqual(svc.recorded_infra_fingerprint(), svc.settings.infra.fingerprint())
        self.assertIn("running and current: keeping it", err.getvalue())

    def test_a_local_build_on_other_settings_still_needs_images(self) -> None:
        # Not just the opt-in differs: the pair would be restarted, so this is
        # a boot that needs a release or `--build`.
        self._record(InfraSettings(infra_build_local=True, egress_port=1))
        svc = _Service(Settings())
        with self._matches(False), patch.object(svc, "is_healthy", return_value=True), \
                self.assertRaises(Die):
            self._ensure(svc, inherit_build=False)
        self.assertEqual([], svc.brought_up_with)
        self.assertFalse(svc.stopped)

    def test_a_forced_restart_still_needs_images(self) -> None:
        self._record(_LOCAL.infra)
        svc = _Service(Settings())
        with self._matches(False), patch.object(svc, "is_healthy", return_value=True), \
                self.assertRaises(Die):
            svc.require_infra_images(inherit_build=False, adopting=False)

    def test_a_matching_release_ignores_an_old_opt_in(self) -> None:
        self._record(_LOCAL.infra)
        svc = _Service(Settings())
        with self._matches(True):
            self._ensure(svc)
        self.assertFalse(svc.brought_up_with[0].infra.infra_build_local)

    def test_each_backend_checks_its_own_format_and_build_tool(self) -> None:
        self.assertEqual((FORMAT_OCI, BUILD_NEEDS_DOCKER),
                         (InfraService.infra_format, InfraService.infra_build_note))
        self.assertEqual((FORMAT_EXT4, BUILD_NEEDS_DOCKER),
                         (FirecrackerInfraService.infra_format,
                          FirecrackerInfraService.infra_build_note))
        self.assertEqual((FORMAT_OCI, BUILD_WITH_CONTAINER),
                         (MacosInfraService.infra_format, MacosInfraService.infra_build_note))
        svc = FirecrackerInfraService(Settings())
        with patch.object(infra_service_mod, "published_infra_matches",
                          return_value=True) as matches:
            svc.require_infra_images()
        matches.assert_called_once_with(FORMAT_EXT4)


class TestOneOrchestratorPerEnvironment(_BringUp):
    """A bring-up records its backend's orchestrator as the environment's, and
    refuses while another backend's recorded one still answers."""

    def _other(self) -> _Service:
        svc = _Service(Settings())
        svc.name = "other"
        return svc

    def test_a_bring_up_records_the_environments_orchestrator(self) -> None:
        with self._matches(True):
            self._ensure(_Service(Settings()))
        self.assertEqual(OrchestratorEndpoint("fake", "http://orch"),
                         recorded_orchestrator(self.root))

    def test_a_second_backend_is_refused_while_the_first_answers(self) -> None:
        with self._matches(True):
            self._ensure(_Service(Settings()))
        other = self._other()
        with self._matches(True), patch.object(OrchestratorClient, "health", return_value=True), \
                self.assertRaises(Die) as caught:
            self._ensure(other)
        self.assertIn("already runs the fake orchestrator (at http://orch)",
                      caught.exception.message)
        self.assertIn("`bot-bottle down --backend fake`", caught.exception.message)
        self.assertEqual([], other.brought_up_with)
        self.assertEqual("fake", recorded_orchestrator(self.root).backend)  # type: ignore[union-attr]

    def test_a_gone_orchestrator_is_replaced_and_the_same_backend_readopted(self) -> None:
        with self._matches(True):
            self._ensure(_Service(Settings()))
        with self._matches(True), patch.object(OrchestratorClient, "health", return_value=True):
            self._ensure(_Service(Settings()))  # same backend: never refused
        other = self._other()
        with self._matches(True), patch.object(OrchestratorClient, "health", return_value=False):
            self._ensure(other)
        self.assertEqual("other", recorded_orchestrator(self.root).backend)  # type: ignore[union-attr]


class TestFirecracker(unittest.TestCase):
    def test_builds_locally_only_when_asked(self) -> None:
        with patch.object(infra_rootfs, "require_infra_images") as require:
            self.assertFalse(infra_rootfs.builds_locally(InfraSettings()))
            self.assertTrue(infra_rootfs.builds_locally(_LOCAL.infra))
        require.assert_called_with(FORMAT_EXT4, local_build=True)

    def test_no_match_without_opt_in_raises_instead_of_building(self) -> None:
        from bot_bottle.infra_image import InfraImagesUnpublished
        with patch("bot_bottle.infra_image.published_infra_artifact", return_value=None), \
                patch("bot_bottle.infra_image.install_version", return_value=("0.1.4", "")), \
                patch.object(infra_vm, "build_infra_images_with_docker") as build, \
                self.assertRaises(InfraImagesUnpublished):
            infra_vm.ensure_built(Settings())
        build.assert_not_called()

    def test_opt_in_without_docker_dies_clearly(self) -> None:
        with patch.object(infra_vm.shutil, "which", return_value=None), \
                patch.object(infra_vm, "build_infra_images_with_docker") as build, \
                patch.object(infra_vm, "die", side_effect=SystemExit) as die, \
                self.assertRaises(SystemExit):
            infra_vm.ensure_built(_LOCAL)
        build.assert_not_called()
        self.assertIn("needs Docker, and it isn't on PATH", die.call_args.args[0])

    def test_opt_in_with_docker_builds(self) -> None:
        with patch.object(infra_vm.shutil, "which", return_value="/usr/bin/docker"), \
                patch.object(infra_vm, "build_infra_images_with_docker") as build:
            infra_vm.ensure_built(_LOCAL)
        build.assert_called_once_with(no_cache=False)


class TestUpBuildFlag(unittest.TestCase):
    def _up(self, argv: list[str]) -> Settings:
        from bot_bottle.cli.commands import lifecycle
        backend = MagicMock()
        backend.name = "docker"
        with patch.object(lifecycle, "get_bottle_backend", return_value=backend), \
                patch.object(lifecycle, "settings_from_env", return_value=Settings()), \
                patch.object(lifecycle, "ensure_via_daemon", return_value="http://o") as ensure, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, lifecycle.cmd_up(argv))
        return ensure.call_args.args[0]

    def test_build_opts_in(self) -> None:
        self.assertTrue(self._up(["--build"]).infra.infra_build_local)
        self.assertFalse(self._up([]).infra.infra_build_local)

    def test_up_never_inherits_and_checks_before_a_restart_stops(self) -> None:
        from bot_bottle.control.hostctl import orchestrator
        backend = MagicMock()
        backend.name = "docker"
        service = backend.infra_service.return_value
        service.require_infra_images.side_effect = Die(1, "no published infra images")
        with patch("bot_bottle.backend.get_bottle_backend", return_value=backend), \
                self.assertRaises(Die):
            orchestrator.ensure(Settings(), restart=True)
        service.require_infra_images.assert_called_once_with(inherit_build=False, adopting=False)
        service.stop.assert_not_called()
        service.require_infra_images.side_effect = None
        service.ensure_running.return_value = "http://o"
        with patch("bot_bottle.backend.get_bottle_backend", return_value=backend):
            orchestrator.ensure(Settings())
        service.ensure_running.assert_called_once_with(
            inherit_build=False, attach_policy=AttachPolicy.STRICT)


if __name__ == "__main__":
    unittest.main()
