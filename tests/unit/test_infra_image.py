"""Pull or build the infra images: the inputs-hash rule (PRD 0081 / 0085)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

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
        infra_image._note_mismatch.cache_clear()  # pyright: ignore[reportPrivateUsage]
        for target, value in (("current_platform", "linux/amd64"),):
            p = patch.object(infra_image, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": ""})
        env.start()
        self.addCleanup(env.stop)

    def _pin(self, *, manifest: dict[str, ImageEntry] | None, local_hash: str = "h1") -> Artifact | None:
        with patch.object(infra_image, "load_artifacts_manifest_optional", return_value=manifest), \
             patch.object(infra_image.image_inputs, "inputs_hash", return_value=local_hash):
            return infra_image.pinned_infra_artifact("bot-bottle-gateway", FORMAT_OCI)

    def test_pulls_when_built_from_this_installs_code(self) -> None:
        self.assertEqual(_ART, self._pin(manifest=_entry("h1")))

    def test_builds_when_the_code_moved_past_the_release(self) -> None:
        with patch.object(infra_image, "info") as info:
            self.assertIsNone(self._pin(manifest=_entry("h0")))
        self.assertIn("built from different code", info.call_args.args[0])

    def test_builds_before_the_first_release_or_when_asked(self) -> None:
        self.assertIsNone(self._pin(manifest=None))
        with patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": "local"}):
            self.assertIsNone(self._pin(manifest=_entry("h1")))


_PIN_TAG = "bot-bottle-gateway:pin-" + "a" * 16


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
            gateway = dgw.DockerGateway()
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
            gateway = mgw.MacosGateway()
            gateway.ensure_built()
        with patch.object(morch, "pinned_infra_artifact", return_value=_ART), \
             patch.object(morch, "pull_pinned_image", return_value="orch:pin-x"):
            orchestrator = morch.MacosOrchestrator()
            orchestrator.ensure_built()
        self.assertEqual(_PIN_TAG, gateway.image_ref)
        self.assertEqual("orch:pin-x", orchestrator.image_ref)

    def test_docker_orchestrator_builds_without_a_pin(self) -> None:
        from bot_bottle.backend.docker import orchestrator as dorch
        with patch.object(dorch, "pinned_infra_artifact", return_value=None), \
             patch.object(dorch, "pull_pinned_image") as pull, \
             patch.object(dorch, "run_docker", return_value=MagicMock(returncode=0)) as run:
            dorch.DockerOrchestrator().ensure_built()
        pull.assert_not_called()
        self.assertEqual(["docker", "build"], run.call_args.args[0][:2])

    def test_registry_failure_is_a_clean_error(self) -> None:
        from bot_bottle.backend.docker import gateway as dgw
        from bot_bottle.gateway import GatewayError
        from bot_bottle.image_registry import RegistryError
        with patch.object(dgw, "pinned_infra_artifact", return_value=_ART), \
             patch.object(dgw, "pull_pinned_image", side_effect=RegistryError("tag moved")), \
             self.assertRaises(GatewayError):
            dgw.DockerGateway().ensure_built()

    def test_orchestrator_registry_failure_is_an_orchestrator_error(self) -> None:
        from bot_bottle.backend.docker import orchestrator as dorch
        from bot_bottle.image_registry import RegistryError
        from bot_bottle.orchestrator.lifecycle import OrchestratorStartError
        with patch.object(dorch, "pinned_infra_artifact", return_value=_ART), \
             patch.object(dorch, "pull_pinned_image", side_effect=RegistryError("401")), \
             self.assertRaises(OrchestratorStartError):
            dorch.DockerOrchestrator().ensure_built()

    def test_macos_gateway_pulls_with_container_image_pull(self) -> None:
        from bot_bottle.backend.macos_container import gateway as mgw
        with patch.object(mgw, "pinned_infra_artifact", return_value=_ART), \
             patch.object(mgw, "pull_pinned_image") as pull, \
             patch.object(mgw.container_mod, "build_image") as build:
            mgw.MacosGateway("bot-bottle-gateway:latest").ensure_built()
        self.assertEqual(("container", "image", "pull"), pull.call_args.kwargs["pull_argv"])
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
