from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.release_bundle import (
    build_bundle_index,
    canonical_bytes,
    parse_bundle_index,
)
from bot_bottle.release_manifest import ReleaseManifestError
from tests.unit.test_release_manifest import manifest


class TestReleaseBundle(unittest.TestCase):
    def test_builds_commit_addressed_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "bot_bottle-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            index = build_bundle_index(
                manifest(),
                wheel=wheel,
                wheel_url=f"https://packages.example/{wheel.name}",
                workflow_run="actions/123",
                published_at="2026-07-27T00:00:00Z",
            )
        self.assertEqual("1" * 40, index["source_commit"])
        self.assertEqual(64, len(index["wheel"]["sha256"]))
        self.assertEqual([], index["qualifications"])
        self.assertIs(index, parse_bundle_index(index))
        self.assertTrue(canonical_bytes(index).endswith(b"\n"))

    def test_rejects_mutable_or_mismatched_wheel_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "release.whl"
            wheel.write_bytes(b"wheel")
            with self.assertRaisesRegex(ReleaseManifestError, "HTTPS"):
                build_bundle_index(
                    manifest(),
                    wheel=wheel,
                    wheel_url="http://packages.example/other.whl",
                    workflow_run="actions/123",
                    published_at="now",
                )

    def test_rejects_index_with_changed_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "release.whl"
            wheel.write_bytes(b"wheel")
            index = build_bundle_index(
                manifest(),
                wheel=wheel,
                wheel_url=f"https://packages.example/{wheel.name}",
                workflow_run="actions/123",
                published_at="now",
            )
        index["oci"]["gateway"] = "registry/gateway:latest"
        with self.assertRaisesRegex(ReleaseManifestError, "digest-pinned"):
            parse_bundle_index(index)
