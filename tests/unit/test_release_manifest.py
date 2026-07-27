from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.release_manifest import (
    ReleaseManifestError,
    oci_image,
    packaged_manifest,
    parse_manifest,
)


def manifest() -> dict[str, object]:
    return {
        "schema": 1,
        "source_commit": "1" * 40,
        "oci": {
            "orchestrator": "registry/orchestrator@sha256:" + "a" * 64,
            "gateway": "registry/gateway@sha256:" + "b" * 64,
        },
        "firecracker": {
            "orchestrator": {"version": "orch-v1", "sha256": "c" * 64},
            "gateway": {"version": "gateway-v1", "sha256": "d" * 64},
        },
    }


class TestParseManifest(unittest.TestCase):
    def test_parses_all_backend_artifacts(self) -> None:
        parsed = parse_manifest(manifest())
        self.assertTrue(parsed.orchestrator_image.endswith("a" * 64))
        self.assertEqual("orch-v1", parsed.firecracker_orchestrator.version)

    def test_rejects_mutable_oci_reference(self) -> None:
        data = manifest()
        assert isinstance(data["oci"], dict)
        data["oci"]["orchestrator"] = "registry/orchestrator:latest"
        with self.assertRaisesRegex(ReleaseManifestError, "digest-pinned"):
            parse_manifest(data)

    def test_rejects_malformed_firecracker_checksum(self) -> None:
        data = manifest()
        assert isinstance(data["firecracker"], dict)
        artifact = data["firecracker"]["gateway"]
        assert isinstance(artifact, dict)
        artifact["sha256"] = "nope"
        with self.assertRaisesRegex(ReleaseManifestError, "64 lowercase hex"):
            parse_manifest(data)

    def test_rejects_missing_manifest_sections(self) -> None:
        cases = (
            (None, "schema 1"),
            ({"schema": 1}, "source_commit"),
            (
                {"schema": 1, "source_commit": "1" * 40},
                "oci must be an object",
            ),
        )
        for data, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ReleaseManifestError, message):
                    parse_manifest(data)

    def test_rejects_malformed_firecracker_artifact(self) -> None:
        data = manifest()
        assert isinstance(data["firecracker"], dict)
        data["firecracker"]["orchestrator"] = None
        with self.assertRaisesRegex(ReleaseManifestError, "must be an object"):
            parse_manifest(data)

        data["firecracker"]["orchestrator"] = {
            "version": " ",
            "sha256": "c" * 64,
        }
        with self.assertRaisesRegex(ReleaseManifestError, "version must be non-empty"):
            parse_manifest(data)


class TestImageSelection(unittest.TestCase):
    def test_development_marker_selects_local_builds(self) -> None:
        with patch(
            "bot_bottle.release_manifest.resources.is_source_checkout",
            return_value=False,
        ), patch(
            "bot_bottle.release_manifest.manifest_path",
        ) as path:
            path.return_value.read_text.return_value = (
                '{"schema": 1, "development": true}')
            self.assertIsNone(packaged_manifest())

    def test_packaged_install_uses_manifest_reference_without_build(self) -> None:
        parsed = parse_manifest(manifest())
        with patch(
            "bot_bottle.release_manifest.packaged_manifest", return_value=parsed,
        ), patch(
            "bot_bottle.release_manifest.local_build_requested", return_value=False,
        ), patch.dict("os.environ", {}, clear=True):
            ref, local = oci_image("orchestrator", "local:latest")
        self.assertEqual(parsed.orchestrator_image, ref)
        self.assertFalse(local)

    def test_mutable_override_fails_outside_local_mode(self) -> None:
        parsed = parse_manifest(manifest())
        with patch(
            "bot_bottle.release_manifest.packaged_manifest", return_value=parsed,
        ), patch(
            "bot_bottle.release_manifest.local_build_requested", return_value=False,
        ), patch.dict(
            "os.environ", {"BOT_BOTTLE_ORCHESTRATOR_IMAGE": "local:latest"},
            clear=True,
        ):
            with self.assertRaisesRegex(ReleaseManifestError, "digest-pinned"):
                oci_image("orchestrator", "fallback:latest")
