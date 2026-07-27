"""Unit tests for shared gateway stdlib HTTP resource boundaries."""
# pylint: disable=protected-access

from __future__ import annotations

import io
import socket
import unittest

from bot_bottle.gateway.bounded_http import (
    BodyReadError,
    BoundedThreadingHTTPServer,
    read_declared_body,
)


class _Handler:
    pass


class _TimeoutStream:
    def read(self, _size: int = -1, /) -> bytes:
        raise TimeoutError


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
