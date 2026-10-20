"""The host controller daemon's HTTP server (stdlib only)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from ...contract.hostctl import (
    AUTH_HEADER, AUTH_SCHEME, HOST_CONTROL, JOBS_PATH, STATUS_PATH, status_body,
)
from ...lib.log import debug
from .jobs import JobRegistry


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
        self.jobs = JobRegistry()

    def role_of(self, authorization: str | None) -> str | None:
        """The `HOST_CONTROL` role an `Authorization` header carries, or None."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != AUTH_SCHEME or not token:
            return None
        return HOST_CONTROL.verify(token.strip(), self.key)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        if url.path == STATUS_PATH:
            self._send(200, status_body(build_id=server.build_id, pid=os.getpid()))
        elif parts and f"/{parts[0]}" == JOBS_PATH:
            self._jobs_route(server, parts[1:], parse_qs(url.query))
        else:
            self._send(404, {"error": f"no route {url.path}"})

    def _jobs_route(self, server: HostControlServer, rest: list[str],
                    query: dict[str, list[str]]) -> None:
        role = self._role()
        if role is None:
            self._send(401, {"error": "missing or invalid token"})
            return
        if not rest:
            self._send(200, {"jobs": [j.summary() for j in server.jobs.list(role)]})
            return
        job = server.jobs.get(rest[0], role)
        if job is None:
            self._send(404, {"error": f"no job {rest[0]}"})
        elif len(rest) == 1:
            self._send(200, job.summary())
        elif rest[1:] == ["events"]:
            start = query.get("from", ["0"])[0]
            self._stream(job.follow(int(start) if start.isdigit() else 0))
        else:
            self._send(404, {"error": "no such job route"})

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

    def _stream(self, events: Iterable[dict[str, object]]) -> None:
        """Newline-delimited JSON until the job ends; closing ends the body."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(json.dumps(event).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client detached; the job carries on

    def log_message(self, format: str, *args: object) -> None:  # pylint: disable=redefined-builtin
        debug(f"hostctl: {self.address_string()} {format % args}")


__all__ = ["HostControlServer"]
