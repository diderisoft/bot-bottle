"""Tests for release qualification and channel promotion."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from bot_bottle.release_manifest import ReleaseManifestError
from bot_bottle.release_qualification import (
    build_release_pointer,
    publish_qualification,
    release_channel,
)

COMMIT = "a" * 40


def bundle() -> bytes:
    return json.dumps({
        "schema": 1,
        "source_commit": COMMIT,
        "wheel": {
            "filename": "bot_bottle.whl",
            "url": (
                "https://gitea.dideric.is/api/packages/didericis/generic/"
                f"bot-bottle-builds/{COMMIT}/bot_bottle.whl"
            ),
            "sha256": "b" * 64,
        },
        "oci": {
            role: f"registry.example/{role}@sha256:{'c' * 64}"
            for role in (
                "orchestrator",
                "gateway",
                "agent_claude",
                "agent_codex",
                "agent_pi",
            )
        },
        "firecracker": {
            role: {"version": "v1", "sha256": "d" * 64}
            for role in ("orchestrator", "gateway")
        },
        "provenance": {"workflow_run": "run", "published_at": "now"},
        "qualifications": [],
    }).encode()


class ReleaseQualificationTest(unittest.TestCase):
    def test_tag_selects_channel(self) -> None:
        self.assertEqual(release_channel("v1.2.3"), "production")
        self.assertEqual(release_channel("v1.2.3-rc.4"), "staging")
        with self.assertRaises(ReleaseManifestError):
            release_channel("latest")

    def test_builds_qualified_pointer(self) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="https://example.test/run/1",
            qualified_at="2026-07-27T00:00:00Z",
        )
        self.assertEqual(pointer["channel"], "production")
        self.assertTrue(pointer["qualified"])

    @mock.patch("bot_bottle.release_qualification.upload")
    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_publishes_bundle_release_and_channel(
        self, remote: mock.Mock, upload: mock.Mock,
    ) -> None:
        remote.side_effect = [bundle(), None, None]
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        publish_qualification(pointer)
        self.assertEqual(upload.call_count, 2)

    @mock.patch("bot_bottle.release_qualification.upload")
    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_rejects_non_monotonic_channel(
        self, remote: mock.Mock, _upload: mock.Mock,
    ) -> None:
        old = build_release_pointer(
            tag="v2.0.0",
            source_commit="b" * 40,
            workflow_run="old-run",
            qualified_at="old-time",
        )
        remote.side_effect = [bundle(), None, json.dumps(old).encode()]
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        with self.assertRaisesRegex(ReleaseManifestError, "not monotonic"):
            publish_qualification(pointer)


if __name__ == "__main__":
    unittest.main()
