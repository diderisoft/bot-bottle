"""Unit: the infra-artifact publisher's upload path (PRD 0069 Stage 2).

The rootfs is hundreds of MB, so `_put` must stream it from disk rather than
read it into memory. Network is mocked; no Docker, no real build.
"""

from __future__ import annotations

import hashlib
from email.message import Message
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.backend.firecracker import publish_infra as pub


class _Resp:
    status = 201

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class TestPut(unittest.TestCase):
    def test_streams_file_body_with_content_length(self) -> None:
        captured: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, *a: object, **k: object) -> _Resp:
            captured.append(req)
            return _Resp()

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "rootfs.ext4.gz"
            payload = b"x" * 4096
            f.write_bytes(payload)
            with mock.patch.object(pub.urllib.request, "urlopen", fake_urlopen):
                pub._put("https://reg/pkg", f, token="t")

        req = captured[0]
        # Body is the open file object (streamed), never the bytes in memory.
        self.assertTrue(hasattr(req.data, "read"))
        self.assertNotIsInstance(req.data, (bytes, bytearray))
        self.assertEqual(str(len(payload)), req.get_header("Content-length"))

    def test_small_bytes_body_still_works(self) -> None:
        captured: list[urllib.request.Request] = []

        def fake_urlopen(req: urllib.request.Request, *a: object, **k: object) -> _Resp:
            captured.append(req)
            return _Resp()

        with mock.patch.object(pub.urllib.request, "urlopen", fake_urlopen):
            pub._put("https://reg/sha", b"abc123  rootfs\n", token="")
        self.assertEqual(b"abc123  rootfs\n", captured[0].data)


class TestPublishBundle(unittest.TestCase):
    def _bundle(self, root: Path, version: str) -> None:
        payload = b"candidate"
        (root / "version.txt").write_text(version + "\n")
        (root / "rootfs.ext4.gz").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (root / "rootfs.ext4.gz.sha256").write_text(
            f"{digest}  rootfs.ext4.gz\n")

    def test_existing_identical_artifact_is_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root, "v1")
            sha = (root / "rootfs.ext4.gz.sha256").read_bytes()
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = sha
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="v1"
            ), mock.patch.object(
                pub.urllib.request, "urlopen", return_value=response
            ), mock.patch.object(pub, "_put") as put:
                self.assertEqual("v1", pub._publish_bundle(root, "token"))
            put.assert_not_called()

    def test_partial_artifact_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root, "v1")
            missing = urllib.error.HTTPError("u", 404, "missing", Message(), None)
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="v1"
            ), mock.patch.object(
                pub.urllib.request, "urlopen", side_effect=missing
            ), mock.patch.object(pub, "_delete") as delete, \
                    mock.patch.object(pub, "_put") as put:
                pub._publish_bundle(root, "token")
            self.assertEqual(3, delete.call_count)
            self.assertEqual(3, put.call_count)

    def test_rejects_bundle_for_different_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root, "old")
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="new"
            ):
                with self.assertRaises(SystemExit) as ctx:
                    pub._publish_bundle(root, "token")
            self.assertIn("does not match checkout", str(ctx.exception))

    def test_rejects_bad_bundle_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root, "v1")
            (root / "rootfs.ext4.gz").write_bytes(b"tampered")
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="v1"
            ):
                with self.assertRaises(SystemExit) as ctx:
                    pub._publish_bundle(root, "token")
            self.assertIn("checksum mismatch", str(ctx.exception))

    def test_registry_lookup_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root, "v1")
            failure = urllib.error.URLError("offline")
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="v1"
            ), mock.patch.object(pub.urllib.request, "urlopen", side_effect=failure):
                with self.assertRaises(SystemExit) as ctx:
                    pub._publish_bundle(root, "token")
            self.assertIn("registry unreachable", str(ctx.exception))


class TestTryDownloadPublished(unittest.TestCase):
    def test_downloads_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(pub.infra_vm, "_infra_init", return_value="init"), \
                mock.patch.object(
                    pub.infra_artifact, "infra_artifact_version", return_value="v1"
                ), mock.patch.object(
                    pub.urllib.request, "urlopen", return_value=_Resp()
                ), mock.patch.object(pub.infra_artifact, "_download") as download:
            root = Path(d)
            result = pub._try_download_published(root)

        self.assertEqual(
            ("v1", root / "rootfs.ext4.gz", root / "rootfs.ext4.gz.sha256"),
            result,
        )
        self.assertEqual(2, download.call_count)

    def test_missing_artifact_returns_none(self) -> None:
        missing = urllib.error.HTTPError("u", 404, "missing", Message(), None)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            pub.urllib.request, "urlopen", side_effect=missing
        ):
            self.assertIsNone(pub._try_download_published(Path(d)))

    def test_registry_http_failure_is_reported(self) -> None:
        failure = urllib.error.HTTPError("u", 500, "failed", Message(), None)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            pub.urllib.request, "urlopen", side_effect=failure
        ):
            with self.assertRaises(SystemExit) as ctx:
                pub._try_download_published(Path(d))
        self.assertIn("registry check failed (HTTP 500)", str(ctx.exception))

    def test_registry_connection_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            pub.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(SystemExit) as ctx:
                pub._try_download_published(Path(d))
        self.assertIn("registry unreachable", str(ctx.exception))


class TestMain(unittest.TestCase):
    def test_output_builds_candidate_and_records_version(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "candidate"
            with mock.patch.object(
                pub, "build_artifact", return_value=("v1", root / "g", root / "s")
            ) as build:
                self.assertEqual(0, pub.main(["--output", str(root)]))
            build.assert_called_once_with(root)
            self.assertEqual("v1\n", (root / "version.txt").read_text())

    def test_output_reuses_published_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "candidate"
            reused = ("v1", root / "rootfs.ext4.gz", root / "rootfs.ext4.gz.sha256")
            with mock.patch.object(
                pub, "_try_download_published", return_value=reused
            ) as reuse, mock.patch.object(pub, "build_artifact") as build:
                self.assertEqual(
                    0, pub.main(["--output", str(root), "--reuse-published"])
                )
            reuse.assert_called_once_with(root)
            build.assert_not_called()
            self.assertEqual("v1\n", (root / "version.txt").read_text())

    def test_publish_dir_publishes_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(pub.infra_artifact, "_config", return_value=("", "", "t")), \
                mock.patch.object(pub, "_publish_bundle", return_value="v1") as publish:
            self.assertEqual(0, pub.main(["--publish-dir", d]))
            publish.assert_called_once_with(Path(d), "t")


if __name__ == "__main__":
    unittest.main()
