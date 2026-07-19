"""Unit: the infra-artifact publisher's upload path (PRD 0069 Stage 2).

The rootfs is hundreds of MB, so `_put` must stream it from disk rather than
read it into memory. Network is mocked; no Docker, no real build.
"""

from __future__ import annotations

import hashlib
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
            missing = urllib.error.HTTPError("u", 404, "missing", {}, None)
            with mock.patch.object(
                pub.infra_artifact, "infra_artifact_version", return_value="v1"
            ), mock.patch.object(
                pub.urllib.request, "urlopen", side_effect=missing
            ), mock.patch.object(pub, "_delete") as delete, \
                    mock.patch.object(pub, "_put") as put:
                pub._publish_bundle(root, "token")
            self.assertEqual(3, delete.call_count)
            self.assertEqual(3, put.call_count)


if __name__ == "__main__":
    unittest.main()
