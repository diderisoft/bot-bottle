"""The host controller daemon's HTTP server (stdlib only)."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from ...contract.hostctl import STATUS_PATH, status_body
from ...lib.log import debug


class HostControlServer(ThreadingHTTPServer):
    """Serves the host controller's routes. `build_id` is what `/status` reports."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], build_id: str) -> None:
        super().__init__(address, _Handler)
        self.build_id = build_id


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        if self.path == STATUS_PATH:
            self._send(200, status_body(build_id=server.build_id, pid=os.getpid()))
        else:
            self._send(404, {"error": f"no route {self.path}"})

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
