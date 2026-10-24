"""Relay a remote viewer's WebSocket to the host controller's attach stream.
Frames pass through unchanged; the host controller decides whether the viewer
may type.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable
from typing import BinaryIO

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..lib import websocket as ws
from .hostctl_relay import HostControlRelay, HostControlUnreachable


async def pump_attach(websocket: WebSocket, relay: HostControlRelay, session_id: str, *,
                      cols: int, rows: int) -> None:
    """Accept `websocket` and pump it to `session_id`'s attach stream until
    either end closes."""
    try:
        sock, reader, writer = await asyncio.to_thread(
            relay.attach, session_id, cols=cols, rows=rows)
    except (HostControlUnreachable, ws.WebSocketError) as e:
        await websocket.close(code=1011, reason=str(e)[:120])
        return
    await websocket.accept()
    lock = threading.Lock()

    def send_up(opcode: int, payload: bytes) -> None:
        with lock:
            ws.write_frame(writer, opcode, payload, mask=True)

    tasks = [asyncio.create_task(_down(websocket, reader, send_up)),
             asyncio.create_task(_up(websocket, send_up))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        _close(sock)
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed


_SendUp = Callable[[int, bytes], None]


async def _down(websocket: WebSocket, reader: BinaryIO, send_up: _SendUp) -> None:
    """Host controller -> viewer."""
    while True:
        try:
            opcode, payload = await asyncio.to_thread(ws.read_frame, reader)
        except (EOFError, OSError, ws.WebSocketError):
            return
        if opcode == ws.CLOSE:
            return
        if opcode == ws.PING:
            await asyncio.to_thread(send_up, ws.PONG, payload)
        elif opcode == ws.BINARY:
            await websocket.send_bytes(payload)
        elif opcode == ws.TEXT:
            await websocket.send_text(payload.decode(errors="replace"))


async def _up(websocket: WebSocket, send_up: _SendUp) -> None:
    """Viewer -> host controller."""
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            return
        if message["type"] == "websocket.disconnect":
            return
        data, text = message.get("bytes"), message.get("text")
        if data is not None:
            opcode, payload = ws.BINARY, bytes(data)
        elif text is not None:
            opcode, payload = ws.TEXT, str(text).encode()
        else:
            continue
        try:
            await asyncio.to_thread(send_up, opcode, payload)
        except OSError:
            return


def _close(sock: socket.socket) -> None:
    """Close the host controller connection, waking a blocked read."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


__all__ = ["pump_attach"]
