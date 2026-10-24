"""Resource limits for the gateway's stdlib HTTP services."""

from __future__ import annotations

import http.server
import io
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class Writable(Protocol):
    def write(self, data: bytes, /) -> object: ...


@dataclass(frozen=True)
class BodyReadError(Exception):
    status: int
    message: str


def read_declared_body(
    stream: Readable,
    connection: socket.socket,
    raw_length: str | None,
    *,
    maximum: int,
    timeout_seconds: float,
    require_length: bool,
) -> bytes:
    """Validate and read one declared body under a read deadline."""
    output = io.BytesIO()
    copy_declared_body(
        stream, output, connection, raw_length, maximum=maximum,
        timeout_seconds=timeout_seconds, require_length=require_length,
    )
    return output.getvalue()


def copy_declared_body(
    stream: Readable,
    output: Writable,
    connection: socket.socket,
    raw_length: str | None,
    *,
    maximum: int,
    timeout_seconds: float,
    require_length: bool,
) -> int:
    """Copy one declared body to a sink without holding it in memory."""
    if raw_length is None:
        if require_length:
            raise BodyReadError(411, "Content-Length required")
        raw_length = "0"
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise BodyReadError(400, "invalid Content-Length") from exc
    if length < 0:
        raise BodyReadError(400, "invalid Content-Length")
    if length > maximum:
        raise BodyReadError(413, "request body too large")
    previous_timeout = connection.gettimeout()
    deadline = time.monotonic() + timeout_seconds
    remaining = length
    try:
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise BodyReadError(408, "request body read timed out")
            connection.settimeout(timeout)
            chunk = stream.read(min(remaining, 64 * 1024))
            if not chunk:
                raise BodyReadError(400, "incomplete request body")
            output.write(chunk)
            remaining -= len(chunk)
    except TimeoutError as exc:
        raise BodyReadError(408, "request body read timed out") from exc
    finally:
        connection.settimeout(previous_timeout)
    return length


class BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on in-flight request threads."""

    daemon_threads = True

    def __init__(  # pylint: disable=consider-using-with
        self, *args, max_workers: int = 32, **kwargs,  # type: ignore[no-untyped-def]
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._request_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(
        self, request: Any, client_address: Any,
    ) -> None:
        if not self._request_slots.acquire(  # pylint: disable=consider-using-with
            blocking=False,
        ):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(
        self, request: Any, client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


__all__ = [
    "BodyReadError",
    "BoundedThreadingHTTPServer",
    "copy_declared_body",
    "read_declared_body",
]
