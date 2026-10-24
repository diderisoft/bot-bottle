"""Minimal RFC 6455 framing over a socket's file objects (stdlib only): the
handshake key and single-frame reads and writes. Fragments are joined; no
extensions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
from typing import BinaryIO, cast

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME_BYTES = 1 << 20


class WebSocketError(Exception):
    """The peer sent something this module won't accept."""


def accept_key(key: str) -> str:
    """`Sec-WebSocket-Accept` for a client's `Sec-WebSocket-Key`."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def client_key() -> str:
    return base64.b64encode(os.urandom(16)).decode()


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    data = stream.read(n)
    if len(data) != n:
        raise EOFError("connection closed")
    return data


def read_frame(stream: BinaryIO) -> tuple[int, bytes]:
    """The next message as (opcode, payload), joining continuation frames."""
    opcode, chunks = 0, []
    while True:
        b0, b1 = _read_exact(stream, 2)
        fin, op = b0 & 0x80, b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", _read_exact(stream, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(stream, 8))[0]
        if length > MAX_FRAME_BYTES:
            raise WebSocketError(f"frame of {length} bytes is too large")
        mask = _read_exact(stream, 4) if b1 & 0x80 else b""
        payload = _read_exact(stream, length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if op >= 0x8:  # control frames are never fragmented
            return op, payload
        opcode = opcode or op
        chunks.append(payload)
        if fin:
            return opcode, b"".join(chunks)


def write_frame(stream: BinaryIO, opcode: int, payload: bytes, *, mask: bool = False) -> None:
    """Write one final frame. Clients must `mask`; servers must not."""
    header = bytearray([0x80 | opcode])
    flag = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        header.append(flag | n)
    elif n < 1 << 16:
        header += bytes([flag | 126]) + struct.pack("!H", n)
    else:
        header += bytes([flag | 127]) + struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        header += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    stream.write(bytes(header) + payload)
    stream.flush()


def connect(host: str, port: int, path: str, headers: dict[str, str] | None = None,
            timeout: float | None = 10.0) -> tuple[socket.socket, BinaryIO, BinaryIO]:
    """Open a client WebSocket; returns (socket, reader, writer). Written
    frames must be masked."""
    sock = socket.create_connection((host, port), timeout=timeout)
    key = client_key()
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
             *(f"{k}: {v}" for k, v in (headers or {}).items())]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    reader = sock.makefile("rb")
    status = reader.readline().decode(errors="replace")
    response: dict[str, str] = {}
    while (line := reader.readline().decode(errors="replace").strip()):
        name, _, value = line.partition(":")
        response[name.strip().lower()] = value.strip()
    if " 101 " not in status or response.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WebSocketError(f"upgrade refused: {status.strip()}")
    return sock, cast(BinaryIO, reader), cast(BinaryIO, sock.makefile("wb", buffering=0))


__all__ = [
    "BINARY", "CLOSE", "PING", "PONG", "TEXT", "WebSocketError",
    "accept_key", "client_key", "connect", "read_frame", "write_frame",
]
