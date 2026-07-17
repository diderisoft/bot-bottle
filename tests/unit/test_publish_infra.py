"""Unit: the infra-artifact publisher's upload path (PRD 0069 Stage 2).

The rootfs is hundreds of MB, so `_put` must stream it from disk rather than
read it into memory. Network is mocked; no Docker, no real build.
"""

from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
