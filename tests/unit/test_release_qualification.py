"""Tests for release qualification and channel promotion."""

from __future__ import annotations

import json
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from bot_bottle.release_manifest import ReleaseManifestError
from bot_bottle.release_qualification import (
    build_release_pointer,
    canonical_pointer,
    delete,
    parse_release_pointer,
    publish_qualification,
    release_channel,
    release_version,
)

COMMIT = "a" * 40


def bundle(source_commit: str = COMMIT) -> bytes:
    return json.dumps({
        "schema": 1,
        "source_commit": source_commit,
        "wheel": {
            "filename": "bot_bottle.whl",
            "url": (
                "https://gitea.dideric.is/api/packages/didericis/generic/"
                f"bot-bottle-builds/{source_commit}/bot_bottle.whl"
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

    def test_release_versions_are_monotonic(self) -> None:
        self.assertLess(
            release_version("v1.2.3-rc.4"), release_version("v1.2.3"))
        self.assertLess(
            release_version("v1.2.3"), release_version("v2.0.0"))
        with self.assertRaises(ReleaseManifestError):
            release_version("v1")

    def test_builds_qualified_pointer(self) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="https://example.test/run/1",
            qualified_at="2026-07-27T00:00:00Z",
        )
        self.assertEqual(pointer["channel"], "production")
        self.assertTrue(pointer["qualified"])
        self.assertTrue(canonical_pointer(pointer).endswith(b"\n"))

    def test_rejects_invalid_pointer_fields(self) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        changes = (
            ("schema", 2),
            ("qualified", False),
            ("channel", "staging"),
            ("source_commit", "short"),
            ("bundle_index_url", "https://wrong.example/index.json"),
            ("qualification", {}),
        )
        for key, value in changes:
            with self.subTest(key=key), self.assertRaises(ReleaseManifestError):
                parse_release_pointer({**pointer, key: value})

    def test_rejects_invalid_build_inputs(self) -> None:
        with self.assertRaises(ReleaseManifestError):
            build_release_pointer(
                tag="v1.2.3",
                source_commit="short",
                workflow_run="run",
                qualified_at="now",
            )
        with self.assertRaises(ReleaseManifestError):
            build_release_pointer(
                tag="v1.2.3",
                source_commit=COMMIT,
                workflow_run="",
                qualified_at="now",
            )

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
    def test_exact_existing_publication_is_noop(
        self, remote: mock.Mock, upload: mock.Mock,
    ) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        wanted = canonical_pointer(pointer)
        remote.side_effect = [bundle(), wanted, wanted]
        publish_qualification(pointer)
        upload.assert_not_called()

    @mock.patch("bot_bottle.release_qualification.upload")
    @mock.patch("bot_bottle.release_qualification.delete")
    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_advances_to_newer_channel(
        self, remote: mock.Mock, remove: mock.Mock, upload: mock.Mock,
    ) -> None:
        old = build_release_pointer(
            tag="v1.0.0",
            source_commit="b" * 40,
            workflow_run="old",
            qualified_at="then",
        )
        pointer = build_release_pointer(
            tag="v2.0.0",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        remote.side_effect = [bundle(), None, canonical_pointer(old)]
        publish_qualification(pointer)
        remove.assert_called_once()
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

    @mock.patch("bot_bottle.release_qualification.upload")
    @mock.patch("bot_bottle.release_qualification.delete")
    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_explicit_rollback_allows_older_channel(
        self, remote: mock.Mock, remove: mock.Mock, _upload: mock.Mock,
    ) -> None:
        old = build_release_pointer(
            tag="v2.0.0",
            source_commit="b" * 40,
            workflow_run="old",
            qualified_at="then",
        )
        pointer = build_release_pointer(
            tag="v1.0.0",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        remote.side_effect = [bundle(), None, canonical_pointer(old)]
        publish_qualification(pointer, allow_rollback=True)
        remove.assert_called_once()

    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_rejects_missing_or_malformed_bundle(self, remote: mock.Mock) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        remote.return_value = None
        with self.assertRaisesRegex(ReleaseManifestError, "does not exist"):
            publish_qualification(pointer)
        remote.return_value = b"not-json"
        with self.assertRaisesRegex(ReleaseManifestError, "malformed"):
            publish_qualification(pointer)
        remote.return_value = bundle("b" * 40)
        with self.assertRaisesRegex(ReleaseManifestError, "does not match"):
            publish_qualification(pointer)

    @mock.patch("bot_bottle.release_qualification.upload")
    @mock.patch("bot_bottle.release_qualification.remote_bytes")
    def test_rejects_changed_release_or_malformed_channel(
        self, remote: mock.Mock, _upload: mock.Mock,
    ) -> None:
        pointer = build_release_pointer(
            tag="v1.2.3",
            source_commit=COMMIT,
            workflow_run="run",
            qualified_at="now",
        )
        remote.side_effect = [bundle(), b"different"]
        with self.assertRaisesRegex(ReleaseManifestError, "already differs"):
            publish_qualification(pointer)
        remote.side_effect = [bundle(), None, b"not-json"]
        with self.assertRaisesRegex(ReleaseManifestError, "malformed"):
            publish_qualification(pointer)

    @mock.patch("bot_bottle.release_qualification.urllib.request.urlopen")
    def test_delete_handles_success_and_not_found(self, urlopen: mock.Mock) -> None:
        urlopen.return_value.__enter__.return_value = mock.Mock()
        delete("https://example.test/channel")
        urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "missing", Message(), None)
        delete("https://example.test/channel")

    @mock.patch("bot_bottle.release_qualification.urllib.request.urlopen")
    def test_delete_reports_registry_errors(self, urlopen: mock.Mock) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "url", 500, "broken", Message(), None)
        with self.assertRaisesRegex(ReleaseManifestError, "HTTP 500"):
            delete("https://example.test/channel")
        urlopen.side_effect = urllib.error.URLError("offline")
        with self.assertRaisesRegex(ReleaseManifestError, "offline"):
            delete("https://example.test/channel")


if __name__ == "__main__":
    unittest.main()
