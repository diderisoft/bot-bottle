"""Unit tests for shared gateway stdlib HTTP resource boundaries."""
# pylint: disable=protected-access

from __future__ import annotations

import io
import socket
import unittest

from bot_bottle.gateway.bounded_http import (
    BodyReadError,
    BoundedThreadingHTTPServer,
    copy_chunked_body,
    read_declared_body,
)


class _Handler:
    pass


class _TimeoutStream:
    def read(self, _size: int = -1, /) -> bytes:
        raise TimeoutError


class TestChunkedBody(unittest.TestCase):
    def setUp(self) -> None:
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)

    def decode(self, raw: bytes, maximum: int = 100) -> bytes:
        out = io.BytesIO()
        n = copy_chunked_body(io.BytesIO(raw), out, self.left, maximum=maximum,
                              timeout_seconds=5)
        self.assertEqual(n, len(out.getvalue()))
        return out.getvalue()

    def test_decodes_chunks_extensions_and_trailers(self) -> None:
        self.assertEqual(b"hello world", self.decode(
            b"5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-Trailer: y\r\n\r\n"))
        self.assertEqual(b"", self.decode(b"0\r\n\r\n"))

    def test_rejects_bad_bodies(self) -> None:
        for raw, why in ((b"zz\r\nhello\r\n0\r\n\r\n", "invalid"),
                         (b"5\r\nhelloXX0\r\n\r\n", "invalid"),
                         (b"5\r\nhel", "invalid|incomplete"),
                         (b"-1\r\n", "invalid"),
                         (b"5" * 2000 + b"\r\n", "invalid")):
            with self.subTest(raw=raw[:20]), self.assertRaisesRegex(BodyReadError, why):
                self.decode(raw)

    def test_size_cap_and_timeout(self) -> None:
        with self.assertRaisesRegex(BodyReadError, "too large"):
            self.decode(b"a\r\n0123456789\r\n0\r\n\r\n", maximum=5)

        class _Slow:
            def read(self, _size: int = -1, /) -> bytes:
                raise TimeoutError

            def readline(self, _size: int = -1, /) -> bytes:
                raise TimeoutError

        with self.assertRaisesRegex(BodyReadError, "timed out"):
            copy_chunked_body(_Slow(), io.BytesIO(), self.left, maximum=10,
                              timeout_seconds=5)


class TestDeclaredBody(unittest.TestCase):
    def setUp(self) -> None:
        self.left, self.right = socket.socketpair()

    def tearDown(self) -> None:
        self.left.close()
        self.right.close()

    def test_rejects_incomplete_body(self) -> None:
        with self.assertRaisesRegex(BodyReadError, "incomplete"):
            read_declared_body(
                io.BytesIO(b"short"),
                self.left,
                "10",
                maximum=100,
                timeout_seconds=1,
                require_length=True,
            )

    def test_maps_read_timeout(self) -> None:
        with self.assertRaises(BodyReadError) as caught:
            read_declared_body(
                _TimeoutStream(),
                self.left,
                "1",
                maximum=100,
                timeout_seconds=1,
                require_length=True,
            )
        self.assertEqual(408, caught.exception.status)


class TestBoundedServer(unittest.TestCase):
    def test_saturated_server_rejects_without_spawning_thread(self) -> None:
        client, peer = socket.socketpair()
        with BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), _Handler, max_workers=1,  # type: ignore[arg-type]
        ) as server:
            with client, peer:
                # Directly reserve the only slot to model an in-flight handler.
                self.assertTrue(
                    server._request_slots.acquire(  # pylint: disable=consider-using-with
                        blocking=False,
                    ),
                )
                try:
                    server.process_request(client, ("127.0.0.1", 1))
                    self.assertIn(b"503 Service Unavailable", peer.recv(1024))
                finally:
                    server._request_slots.release()


if __name__ == "__main__":
    unittest.main()
