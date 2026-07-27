from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.release_bundle import build_bundle_index, canonical_bytes
from bot_bottle.release_manifest import ReleaseManifestError
from bot_bottle.release_publish import bundle_url, publish_bundle
from tests.unit.test_release_manifest import manifest


class TestReleasePublish(unittest.TestCase):
    def setUp(self) -> None:
        env = {
            "BOT_BOTTLE_RELEASE_BASE": "https://packages.example",
            "BOT_BOTTLE_RELEASE_OWNER": "owner",
            "BOT_BOTTLE_RELEASE_TOKEN": "token",
        }
        self.env = patch.dict("os.environ", env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def bundle(self, root: Path) -> tuple[dict[str, object], Path]:
        wheel = root / "bot_bottle-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        index = build_bundle_index(
            manifest(),
            wheel=wheel,
            wheel_url=bundle_url("1" * 40, wheel.name),
            workflow_run="actions/123",
            published_at="2026-07-27T00:00:00Z",
        )
        return index, wheel

    @staticmethod
    def wheel_sha(index: dict[str, object]) -> str:
        wheel = index["wheel"]
        assert isinstance(wheel, dict)
        digest = wheel["sha256"]
        assert isinstance(digest, str)
        return digest

    def test_publishes_wheel_then_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index, wheel = self.bundle(Path(tmp))
            with patch(
                "bot_bottle.release_publish.remote_bytes", return_value=None,
            ), patch(
                "bot_bottle.release_publish.remote_sha256", return_value=None,
            ), patch("bot_bottle.release_publish.upload") as upload:
                result = publish_bundle(index, wheel)
        self.assertTrue(result.endswith("/bundle-index.json"))
        self.assertEqual(2, upload.call_count)
        self.assertIs(wheel, upload.call_args_list[0].args[1])
        self.assertEqual(canonical_bytes(index), upload.call_args_list[1].args[1])

    def test_exact_existing_bundle_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index, wheel = self.bundle(Path(tmp))
            with patch(
                "bot_bottle.release_publish.remote_bytes",
                return_value=canonical_bytes(index),
            ), patch(
                "bot_bottle.release_publish.remote_sha256",
                return_value=self.wheel_sha(index),
            ), patch("bot_bottle.release_publish.upload") as upload:
                publish_bundle(index, wheel)
        upload.assert_not_called()

    def test_rejects_partial_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index, wheel = self.bundle(Path(tmp))
            with patch(
                "bot_bottle.release_publish.remote_bytes", return_value=None,
            ), patch(
                "bot_bottle.release_publish.remote_sha256",
                return_value=self.wheel_sha(index),
            ):
                with self.assertRaisesRegex(
                    ReleaseManifestError, "administrative cleanup",
                ):
                    publish_bundle(index, wheel)

    def test_rejects_existing_bundle_with_different_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index, wheel = self.bundle(Path(tmp))
            with patch(
                "bot_bottle.release_publish.remote_bytes", return_value=b"other",
            ), patch(
                "bot_bottle.release_publish.remote_sha256",
                return_value=self.wheel_sha(index),
            ):
                with self.assertRaisesRegex(
                    ReleaseManifestError, "already differs",
                ):
                    publish_bundle(index, wheel)
