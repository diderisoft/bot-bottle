"""The host controller daemon's HTTP server (stdlib only)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from ...contract.auth import ROLE_CLI
from ...contract.hostctl import (
    AUTH_HEADER, AUTH_SCHEME, BOTTLES_PATH, HOST_CONTROL, JOBS_PATH, ORCHESTRATOR_PATH,
    ROLE_ORCHESTRATOR, SESSIONS_PATH, STATUS_PATH, LaunchRequest, Settings, status_body,
)
from ...lib import websocket as ws
from ...lib.log import debug
from . import orchestrator
from .attach import Viewer
from .bottles import BottleService
from .jobs import JobRegistry
from .sessions import SessionManager

MAX_BODY_BYTES = 1 << 20
# Roles that may launch, list, tear down and watch bottles.
_BOTTLE_ROLES = (ROLE_CLI, ROLE_ORCHESTRATOR)


class HostControlServer(ThreadingHTTPServer):
    """Serves the host controller's routes. `build_id` is what `/status`
    reports; `key` verifies callers' `HOST_CONTROL` tokens. A listener on a
    guest network passes `peers` (only those addresses may connect) and the
    `roles` it accepts; listeners share `jobs`, `sessions` and `bottles`."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], build_id: str, key: str,
                 sessions: SessionManager | None = None,
                 bottles: BottleService | None = None, *,
                 jobs: JobRegistry | None = None, peers: str = "",
                 roles: frozenset[str] = HOST_CONTROL.roles) -> None:
        if not key:
            raise ValueError("host controller needs its signing key")
        super().__init__(address, _Handler)
        self.build_id = build_id
        self.key = key
        self.peers = ipaddress.ip_network(peers) if peers else None
        self.roles = roles
        self.jobs = jobs if jobs is not None else JobRegistry()
        self.sessions = sessions if sessions is not None else SessionManager({})
        self.bottles = bottles if bottles is not None else BottleService(self.sessions)

    def verify_request(self, request: socket.socket | tuple[bytes, socket.socket],
                       client_address: tuple[str, int] | str) -> bool:
        """Drop a connection from outside `peers` before reading anything."""
        if self.peers is None:
            return True
        try:
            host = client_address[0] if isinstance(client_address, tuple) else client_address
            return ipaddress.ip_address(host) in self.peers
        except ValueError:
            return False

    def role_of(self, authorization: str | None) -> str | None:
        """The `HOST_CONTROL` role an `Authorization` header carries, if this
        listener accepts it; otherwise None."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != AUTH_SCHEME or not token:
            return None
        role = HOST_CONTROL.verify(token.strip(), self.key)
        return role if role in self.roles else None


def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
    value = query.get(name, [""])[0]
    return int(value) if value.isdigit() else default


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        if url.path == STATUS_PATH:
            self._send(200, status_body(build_id=server.build_id, pid=os.getpid()))
        elif parts and f"/{parts[0]}" == JOBS_PATH:
            self._jobs_route(server, parts[1:], parse_qs(url.query))
        elif len(parts) == 3 and f"/{parts[0]}" == SESSIONS_PATH and parts[2] == "attach":
            self._attach(server, parts[1], parse_qs(url.query))
        elif len(parts) == 2 and f"/{parts[0]}" == SESSIONS_PATH:
            if self._allowed(_BOTTLE_ROLES):
                self._send(200, server.bottles.session_state(parts[1]))
        elif url.path == BOTTLES_PATH:
            if self._allowed(_BOTTLE_ROLES):
                self._send(200, {"bottles": server.bottles.list()})
        else:
            self._send(404, {"error": f"no route {url.path}"})

    def _attach(self, server: HostControlServer, session_id: str,
                query: dict[str, list[str]]) -> None:
        """Upgrade to a WebSocket and pump a read-only viewer of `session_id`."""
        if self._role() not in (ROLE_CLI, ROLE_ORCHESTRATOR):
            self._send(401, {"error": "missing or invalid token"})
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            self._send(400, {"error": "expected a WebSocket upgrade"})
            return
        record = server.sessions.record(session_id)
        if record is None or not server.sessions.exists(session_id):
            self._send(404, {"error": f"no session {session_id}"})
            return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        Viewer(server.sessions, session_id, self.rfile, self.wfile,  # type: ignore[arg-type]
               writable_allowed=record.writable_allowed).run(
                   cols=_int_param(query, "cols", 80), rows=_int_param(query, "rows", 24))
        self.close_connection = True

    def do_DELETE(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) != 2 or f"/{parts[0]}" != BOTTLES_PATH:
            self._send(404, {"error": f"no route {self.path}"})
            return
        role = self._allowed(_BOTTLE_ROLES)
        if role is None:
            return
        bottle_id = parts[1]
        if not server.bottles.known(bottle_id):
            self._send(404, {"error": f"no bottle {bottle_id}"})
            return
        job = server.jobs.start("bottle.teardown", role,
                                lambda: server.bottles.teardown(bottle_id))
        self._send(202, {"job": job.id})

    def _launch(self, server: HostControlServer) -> None:
        role = self._allowed(_BOTTLE_ROLES)
        if role is None:
            return
        try:
            request = LaunchRequest.from_json(self._json_body(), role)
        except PermissionError as e:
            self._send(403, {"error": str(e)})
            return
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        job = server.jobs.start("bottle.launch", role,
                                lambda: server.bottles.launch(request, role))
        self._send(202, {"job": job.id})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        path = urlsplit(self.path).path
        if path == BOTTLES_PATH:
            self._launch(server)
            return
        action = path.removeprefix(ORCHESTRATOR_PATH + "/")
        if not path.startswith(ORCHESTRATOR_PATH + "/") or \
                action not in ("ensure", "restart", "status"):
            self._send(404, {"error": f"no route {path}"})
            return
        role = self._role()
        if role is None:
            self._send(401, {"error": "missing or invalid token"})
            return
        if role != ROLE_CLI:
            self._send(403, {"error": f"{path} is for the cli role"})
            return
        try:
            settings = Settings.from_json(self._json_body().get("settings"))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        if action == "status":
            self._send(200, orchestrator.status(settings))
            return
        job = server.jobs.start(
            f"orchestrator.{action}", role,
            lambda: orchestrator.ensure(settings, restart=action == "restart"),
        )
        self._send(202, {"job": job.id})

    def _json_body(self) -> dict[str, object]:
        """The request's JSON object body; ValueError if missing, too big or not an object."""
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY_BYTES:
            raise ValueError("request body missing or too large")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

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

    def _allowed(self, roles: tuple[str, ...]) -> str | None:
        """The caller's role if it's one of `roles`; otherwise send 401/403 and
        return None."""
        role = self._role()
        if role is None:
            self._send(401, {"error": "missing or invalid token"})
        elif role not in roles:
            self._send(403, {"error": f"not for the {role} role"})
            return None
        return role

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
