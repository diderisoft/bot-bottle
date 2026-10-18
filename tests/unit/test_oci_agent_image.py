"""PRD 0085: published agent images on the OCI backends (docker, macos_container)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle.agent_overlay import OverlayError
from bot_bottle.backend import oci_agent_image as oai
from bot_bottle.image_registry import RegistryError
from bot_bottle.manifest import ManifestImageOverlay
from bot_bottle.published_artifacts import FORMAT_OCI, Artifact, ArtifactsManifestError

_PIN = Artifact(
    image="bot-bottle-claude", format=FORMAT_OCI, platform="linux/amd64",
    url="reg/o/bot-bottle-claude:0.1.0-amd64", digest="sha256:" + "a" * 64,
)
_PINNED_REF = "reg/o/bot-bottle-claude@sha256:" + "a" * 64
_PIN_TAG = "bot-bottle-claude:pin-" + "a" * 16


class _Store:
    """An in-memory image store standing in for docker / `container`."""

    def __init__(self, present: tuple[str, ...] = ()) -> None:
        self.images = set(present)
        self.built: list[str] = []
        self.verified: list[str] = []

    def runtime(self) -> oai.OciRuntime:
        return oai.OciRuntime(
            pull_argv=("docker", "pull"),
            image_exists=lambda ref: ref in self.images,
            tag_image=lambda src, dst: self.images.add(dst),
            build_image=self._build,
            verify=lambda ref, argv: self.verified.append(ref),
        )

    def _build(self, ref: str, context: str, *, dockerfile: str = "") -> None:
        self.assertions(context, dockerfile)
        self.built.append(ref)
        self.images.add(ref)

    @staticmethod
    def assertions(context: str, dockerfile: str) -> None:
        assert Path(dockerfile).is_file() and Path(dockerfile).parent == Path(context)


def _plan(cwd: str, *, overlay: ManifestImageOverlay = ManifestImageOverlay(),
          custom: bool = False, policy: str = "build") -> Any:
    return cast(Any, SimpleNamespace(
        spec=SimpleNamespace(image_policy=policy, user_cwd=cwd),
        manifest=SimpleNamespace(bottle=SimpleNamespace(agent_provider=SimpleNamespace(
            overlay=overlay, builds_custom_image=custom,
        ))),
    ))


class ResolvePublished(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name

    def _resolve(self, plan: Any, store: _Store, *, pin: Artifact | None = _PIN,
                 pulls: list[str] | None = None) -> str | None:
        def pull(resolved: Any, *, argv: tuple[str, ...]) -> None:
            store.images.add(resolved.pinned_ref)
            if pulls is not None:
                pulls.append(resolved.pinned_ref)

        with mock.patch.object(oai.agent_image, "resolve_agent_pin", return_value=pin), \
             mock.patch.object(oai.image_registry, "resolve",
                               return_value=SimpleNamespace(pinned_ref=_PINNED_REF)), \
             mock.patch.object(oai.image_registry, "pull", side_effect=pull):
            return oai.resolve_published_agent_image(
                plan, store.runtime(), template="claude",
                image="bot-bottle-claude:latest", smoke_test=("claude", "--version"))

    def test_custom_dockerfile_is_never_replaced(self) -> None:
        with mock.patch.object(oai.agent_image, "resolve_agent_pin") as pin:
            self.assertIsNone(oai.resolve_published_agent_image(
                _plan(self.cwd, custom=True), _Store().runtime(), template="claude",
                image="bot-bottle-claude:latest", smoke_test=()))
        pin.assert_not_called()

    def test_nothing_published_builds_locally(self) -> None:
        self.assertIsNone(self._resolve(_plan(self.cwd), _Store(), pin=None))

    def test_overlay_without_a_published_base_fails_closed(self) -> None:
        with self.assertRaises(OverlayError):
            self._resolve(_plan(self.cwd, overlay=ManifestImageOverlay(apt=("jq",))),
                          _Store(), pin=None)

    def test_pulls_tags_by_content_and_smoke_tests(self) -> None:
        store, pulls = _Store(), []
        self.assertEqual(_PIN_TAG, self._resolve(_plan(self.cwd), store, pulls=pulls))
        self.assertEqual([_PINNED_REF], pulls)
        self.assertEqual([_PIN_TAG], store.verified)

    def test_present_image_launches_without_the_registry(self) -> None:
        pulls: list[str] = []
        self.assertEqual(_PIN_TAG, self._resolve(_plan(self.cwd), _Store((_PIN_TAG,)),
                                                 pulls=pulls))
        self.assertEqual([], pulls)

    def test_cached_policy_dies_when_the_derived_tag_is_missing(self) -> None:
        with self.assertRaises(SystemExit):
            self._resolve(_plan(self.cwd, policy="cached"), _Store())

    def test_overlay_builds_once_under_a_content_tag(self) -> None:
        store = _Store()
        plan = _plan(self.cwd, overlay=ManifestImageOverlay(apt=("jq",)))
        tag = self._resolve(plan, store)
        assert tag is not None
        self.assertTrue(tag.startswith("bot-bottle-claude:overlay-"))
        self.assertEqual([tag], store.built)
        self.assertEqual([tag], store.verified)
        self.assertEqual(tag, self._resolve(plan, store))  # reused, not rebuilt
        self.assertEqual([tag], store.built)

    def test_different_overlays_never_share_a_tag(self) -> None:
        # The old shared `:latest` tag let one bottle run another's overlay.
        store = _Store()
        a = self._resolve(_plan(self.cwd, overlay=ManifestImageOverlay(apt=("jq",))), store)
        b = self._resolve(_plan(self.cwd, overlay=ManifestImageOverlay(apt=("curl",))), store)
        plain = self._resolve(_plan(self.cwd), store)
        self.assertEqual(3, len({a, b, plain}))

    def test_protocol_mismatch_dies_with_the_reason(self) -> None:
        with mock.patch.object(oai.agent_image, "resolve_agent_pin",
                               side_effect=ArtifactsManifestError("protocol 2 vs 1")), \
             self.assertRaises(SystemExit):
            oai.resolve_published_agent_image(
                _plan(self.cwd), _Store().runtime(), template="claude",
                image="bot-bottle-claude:latest", smoke_test=())

    def test_registry_failure_dies_with_the_reason(self) -> None:
        with mock.patch.object(oai.agent_image, "resolve_agent_pin", return_value=_PIN), \
             mock.patch.object(oai.image_registry, "resolve",
                               side_effect=RegistryError("tag moved")), \
             self.assertRaises(SystemExit):
            oai.resolve_published_agent_image(
                _plan(self.cwd), _Store().runtime(), template="claude",
                image="bot-bottle-claude:latest", smoke_test=())


if __name__ == "__main__":
    unittest.main()
