"""Pull or build the infra images: the inputs-hash rule (PRD 0081 / 0085)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import InfraSettings, Settings
from bot_bottle import infra_image
from bot_bottle.published_artifacts import FORMAT_OCI, Artifact, ImageEntry

_ART = Artifact(image="bot-bottle-gateway", format=FORMAT_OCI, platform="linux/amd64",
                url="reg/o/bot-bottle-gateway:1.0.0-amd64", digest="sha256:" + "a" * 64)


def _entry(inputs_hash: str) -> dict[str, ImageEntry]:
    return {"bot-bottle-gateway": ImageEntry(
        name="bot-bottle-gateway", version="1.0.0", inputs_hash=inputs_hash,
        artifacts=(_ART,), protocol=1)}


class PinnedInfraArtifact(unittest.TestCase):
    def setUp(self) -> None:
        infra_image._local_inputs_hash.cache_clear()  # pyright: ignore[reportPrivateUsage]
        for target, value in (("current_platform", "linux/amd64"),
                              ("install_version", ("0.1.4", "abcdef012345"))):
            p = patch.object(infra_image, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _pin(
        self, *, manifest: dict[str, ImageEntry] | None, local_hash: str = "h1",
        local_build: bool = False,
    ) -> Artifact | None:
        with patch.object(infra_image, "load_artifacts_manifest_optional", return_value=manifest), \
             patch.object(infra_image.image_inputs, "inputs_hash", return_value=local_hash):
            return infra_image.pinned_infra_artifact(
                "bot-bottle-gateway", FORMAT_OCI, local_build=local_build)

    def test_pulls_when_built_from_this_installs_code(self) -> None:
        self.assertEqual(_ART, self._pin(manifest=_entry("h1")))

    def test_stops_when_the_code_moved_past_the_release(self) -> None:
        # Never a silent local build: the caller is told how to go on.
        with self.assertRaises(infra_image.InfraImagesUnpublished) as caught:
            self._pin(manifest=_entry("h0"))
        self.assertIn("0.1.4 @ abcdef012345, unreleased", str(caught.exception))
        self.assertIn("bot-bottle up --build", str(caught.exception))

    def test_stops_before_the_first_release(self) -> None:
        with self.assertRaises(infra_image.InfraImagesUnpublished):
            self._pin(manifest=None)

    def test_stops_when_nothing_is_published_for_this_platform(self) -> None:
        other = Artifact(image="bot-bottle-gateway", format=FORMAT_OCI, platform="linux/arm64",
                         url="reg/o/g:1-arm64", digest="sha256:" + "b" * 64)
        manifest = {"bot-bottle-gateway": ImageEntry(
            name="bot-bottle-gateway", version="1.0.0", inputs_hash="h1",
            artifacts=(other,), protocol=1)}
        with self.assertRaises(infra_image.InfraImagesUnpublished):
            self._pin(manifest=manifest)

    def test_builds_only_when_asked(self) -> None:
        self.assertIsNone(self._pin(manifest=_entry("h1"), local_build=True))
        self.assertIsNone(self._pin(manifest=None, local_build=True))


class UnpublishedMessage(unittest.TestCase):
    def test_names_the_install_and_both_ways_forward(self) -> None:
        with patch.object(infra_image, "install_version", return_value=("0.1.4", "abcdef012345")):
            message = infra_image.unpublished_message()
        self.assertEqual(
            "this install (0.1.4 @ abcdef012345, unreleased) has no published infra images.\n"
            "    Install a release: uv tool install bot-bottle   (or pipx install bot-bottle)\n"
            "    Or build them locally (needs Docker): bot-bottle up --build\n"
            "    (BOT_BOTTLE_INFRA_BUILD=local does the same for every command.)",
            message)

    def test_without_a_commit_names_the_version_alone(self) -> None:
        with patch.object(infra_image, "install_version", return_value=("0.1.4", "")):
            message = infra_image.unpublished_message(infra_image.BUILD_WITH_CONTAINER)
        self.assertIn("this install (0.1.4, unreleased)", message)
        self.assertIn("Or build them locally (with Apple's `container`): bot-bottle up --build",
                      message)

    def test_published_infra_matches_needs_every_infra_image(self) -> None:
        def artifact(image: str, _fmt: str) -> Artifact | None:
            return _ART if image == "bot-bottle-gateway" else None
        with patch.object(infra_image, "published_infra_artifact", side_effect=artifact):
            self.assertFalse(infra_image.published_infra_matches(FORMAT_OCI))
        with patch.object(infra_image, "published_infra_artifact", return_value=_ART):
            self.assertTrue(infra_image.published_infra_matches(FORMAT_OCI))


_PIN_TAG = "bot-bottle-gateway:pin-" + "a" * 16
_LOCAL = Settings(infra=InfraSettings(infra_build_local=True))


def _unpublished():  # type: ignore[no-untyped-def]
    """Nothing published matches this install."""
    return patch.object(infra_image, "published_infra_artifact", return_value=None)


class PullPinnedImage(unittest.TestCase):
    def test_pulls_by_digest_and_runs_a_tag_naming_only_that_digest(self) -> None:
        # Retagging `:latest` would leave an earlier local build answering to
        # it on Apple Container, so the pulled image gets its own tag.
        tag = MagicMock()
        with patch.object(infra_image.image_registry, "resolve") as resolve, \
             patch.object(infra_image.image_registry, "pull") as pull:
            ref = infra_image.pull_pinned_image(
                _ART, local_ref="bot-bottle-gateway:latest", image_exists=lambda _ref: False,
                tag_image=tag, pull_argv=("docker", "pull"))
        self.assertEqual(_PIN_TAG, ref)
        pull.assert_called_once_with(resolve.return_value, argv=("docker", "pull"))
        tag.assert_called_once_with("reg/o/bot-bottle-gateway@sha256:" + "a" * 64, _PIN_TAG)

    def test_local_digest_skips_the_registry(self) -> None:
        with patch.object(infra_image.image_registry, "resolve") as resolve:
            infra_image.pull_pinned_image(
                _ART, local_ref="bot-bottle-gateway:latest", image_exists=lambda ref: "@" in ref,
                tag_image=MagicMock(), pull_argv=("docker", "pull"))
        resolve.assert_not_called()

    def test_an_existing_pin_tag_is_used_as_is(self) -> None:
        tag = MagicMock()
        with patch.object(infra_image.image_registry, "resolve") as resolve:
            ref = infra_image.pull_pinned_image(
                _ART, local_ref="bot-bottle-gateway:latest", image_exists=lambda r: r == _PIN_TAG,
                tag_image=tag, pull_argv=("docker", "pull"))
        self.assertEqual(_PIN_TAG, ref)
        resolve.assert_not_called()
        tag.assert_not_called()

    def test_the_repo_keeps_a_registry_port(self) -> None:
        ref = infra_image.pull_pinned_image(
            _ART, local_ref="host:5000/bot-bottle-gateway", image_exists=lambda _r: True,
            tag_image=MagicMock(), pull_argv=("docker", "pull"))
        self.assertEqual("host:5000/bot-bottle-gateway:pin-" + "a" * 16, ref)


class BackendsPullOrBuild(unittest.TestCase):
    def test_docker_gateway_pulls_the_pin_instead_of_building(self) -> None:
        from bot_bottle.backend.docker import gateway as dgw
        with patch.object(dgw, "pinned_infra_artifact", return_value=_ART) as pin, \
             patch.object(dgw, "pull_pinned_image", return_value=_PIN_TAG) as pull, \
             patch.object(dgw, "run_docker") as run:
            gateway = dgw.DockerGateway(settings=Settings())
            gateway.ensure_built()
        self.assertEqual("bot-bottle-gateway", pin.call_args.args[0])
        pull.assert_called_once()
        run.assert_not_called()
        self.assertEqual(_PIN_TAG, gateway.image_ref)  # what it runs and compares against

    def test_macos_runs_the_pulled_pin_not_latest(self) -> None:
        from bot_bottle.backend.macos_container import gateway as mgw
        from bot_bottle.backend.macos_container import orchestrator as morch
        with patch.object(mgw, "pinned_infra_artifact", return_value=_ART), \
             patch.object(mgw, "pull_pinned_image", return_value=_PIN_TAG):
            gateway = mgw.MacosGateway(settings=Settings())
            gateway.ensure_built()
        with patch.object(morch, "pinned_infra_artifact", return_value=_ART), \
             patch.object(morch, "pull_pinned_image", return_value="orch:pin-x"):
            orchestrator = morch.MacosOrchestrator(settings=Settings())
            orchestrator.ensure_built()
        self.assertEqual(_PIN_TAG, gateway.image_ref)
        self.assertEqual("orch:pin-x", orchestrator.image_ref)

    def test_docker_orchestrator_builds_when_asked(self) -> None:
        from bot_bottle.backend.docker import orchestrator as dorch
        with patch.object(dorch, "pull_pinned_image") as pull, \
             patch.object(dorch, "run_docker", return_value=MagicMock(returncode=0)) as run:
            dorch.DockerOrchestrator(settings=_LOCAL).ensure_built()
        pull.assert_not_called()
        self.assertEqual(["docker", "build"], run.call_args.args[0][:2])

    def test_docker_gateway_builds_when_asked(self) -> None:
        from bot_bottle.backend.docker import gateway as dgw
        with patch.object(dgw, "pull_pinned_image") as pull, \
             patch.object(dgw, "run_docker", return_value=MagicMock(returncode=0)) as run:
            dgw.DockerGateway(settings=_LOCAL).ensure_built()
        pull.assert_not_called()
        self.assertEqual(["docker", "build"], run.call_args.args[0][:2])

    def test_docker_without_a_match_or_opt_in_stops_without_building(self) -> None:
        from bot_bottle.backend.docker import gateway as dgw
        from bot_bottle.backend.docker import orchestrator as dorch
        from bot_bottle.control.gateway import GatewayError
        from bot_bottle.control.lifecycle import OrchestratorStartError
        with _unpublished(), \
             patch.object(dgw, "run_docker") as gw_run, \
             patch.object(dorch, "run_docker") as orch_run:
            with self.assertRaises(GatewayError) as gw:
                dgw.DockerGateway(settings=Settings()).ensure_built()
            with self.assertRaises(OrchestratorStartError) as orch:
                dorch.DockerOrchestrator(settings=Settings()).ensure_built()
        gw_run.assert_not_called()
        orch_run.assert_not_called()
        for caught in (gw, orch):
            self.assertIn("needs Docker): bot-bottle up --build", str(caught.exception))

    def test_macos_without_a_match_or_opt_in_stops_without_building(self) -> None:
        from bot_bottle.backend.macos_container import gateway as mgw
        from bot_bottle.backend.macos_container import orchestrator as morch
        from bot_bottle.control.gateway import GatewayError
        from bot_bottle.control.lifecycle import OrchestratorStartError
        with _unpublished(), \
             patch.object(mgw.container_mod, "build_image") as build:
            with self.assertRaises(GatewayError) as gw:
                mgw.MacosGateway(settings=Settings()).ensure_built()
            with self.assertRaises(OrchestratorStartError) as orch:
                morch.MacosOrchestrator(settings=Settings()).ensure_built()
        build.assert_not_called()
        for caught in (gw, orch):
            self.assertIn("with Apple's `container`): bot-bottle up --build",
                          str(caught.exception))

    def test_macos_builds_when_asked(self) -> None:
        from bot_bottle.backend.macos_container import gateway as mgw
        with patch.object(mgw.container_mod, "image_exists", return_value=False), \
             patch.object(mgw.container_mod, "build_image") as build, \
             patch.object(mgw.container_mod, "tag_image"), \
             patch.object(mgw, "local_build_tag", return_value="bot-bottle-gateway:src-abc"), \
             patch.object(mgw, "pull_pinned_image") as pull:
            mgw.MacosGateway(settings=_LOCAL).ensure_built()
        pull.assert_not_called()
        build.assert_called_once()

    def test_registry_failure_is_a_clean_error(self) -> None:
        from bot_bottle.backend.docker import gateway as dgw
        from bot_bottle.control.gateway import GatewayError
        from bot_bottle.image_registry import RegistryError
        with patch.object(dgw, "pinned_infra_artifact", return_value=_ART), \
             patch.object(dgw, "pull_pinned_image", side_effect=RegistryError("tag moved")), \
             self.assertRaises(GatewayError):
            dgw.DockerGateway(settings=Settings()).ensure_built()

    def test_orchestrator_registry_failure_is_an_orchestrator_error(self) -> None:
        from bot_bottle.backend.docker import orchestrator as dorch
        from bot_bottle.image_registry import RegistryError
        from bot_bottle.control.lifecycle import OrchestratorStartError
        with patch.object(dorch, "pinned_infra_artifact", return_value=_ART), \
             patch.object(dorch, "pull_pinned_image", side_effect=RegistryError("401")), \
             self.assertRaises(OrchestratorStartError):
            dorch.DockerOrchestrator(settings=Settings()).ensure_built()

    def test_macos_gateway_pulls_with_container_image_pull(self) -> None:
        from bot_bottle.backend.macos_container import gateway as mgw
        with patch.object(mgw, "pinned_infra_artifact", return_value=_ART), \
             patch.object(mgw, "pull_pinned_image") as pull, \
             patch.object(mgw.container_mod, "build_image") as build:
            mgw.MacosGateway("bot-bottle-gateway:latest", settings=Settings()).ensure_built()
        self.assertEqual(("container", "image", "pull"), pull.call_args.kwargs["pull_argv"])
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
