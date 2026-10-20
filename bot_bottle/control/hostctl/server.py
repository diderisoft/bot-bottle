"""The host controller daemon's HTTP server (stdlib only)."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from ...contract.hostctl import AUTH_HEADER, AUTH_SCHEME, HOST_CONTROL, STATUS_PATH, status_body
from ...lib.log import debug


class HostControlServer(ThreadingHTTPServer):
    """Serves the host controller's routes. `build_id` is what `/status`
    reports; `key` verifies callers' `HOST_CONTROL` tokens."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], build_id: str, key: str) -> None:
        if not key:
            raise ValueError("host controller needs its signing key")
        super().__init__(address, _Handler)
        self.build_id = build_id
        self.key = key

    def role_of(self, authorization: str | None) -> str | None:
        """The `HOST_CONTROL` role an `Authorization` header carries, or None."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != AUTH_SCHEME or not token:
            return None
        return HOST_CONTROL.verify(token.strip(), self.key)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        if self.path == STATUS_PATH:
            self._send(200, status_body(build_id=server.build_id, pid=os.getpid()))
        else:
            self._send(404, {"error": f"no route {self.path}"})

    def _role(self) -> str | None:
        """The caller's `HOST_CONTROL` role, from its bearer token."""
        return cast(HostControlServer, self.server).role_of(self.headers.get(AUTH_HEADER))

    def _send(self, code: int, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        debug(f"hostctl: {self.address_string()} {format % args}")


__all__ = ["HostControlServer"]
