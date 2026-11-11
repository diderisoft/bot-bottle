"""The host controller's HTTP server (stdlib only)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sqlite3
from collections.abc import Callable, Iterable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from ...contract.auth import ROLE_CLI
from ...contract.audit import AuditEvent
from ...contract.firehose import FIREHOSE_PATH, FIREHOSE_STREAM_PATH
from ...contract.hostctl import (
    AUDIT_APPEND_PATH, AUDIT_IMPORT_PATH, AUDIT_PASSKEYS_PATH, AUDIT_PRODUCERS, AUDIT_QUERY_PATH,
    AGENT_PREFS_PATH, BOTTLED_AGENTS_PATH, BOTTLED_AGENTS_REMOVE_PATH, INFRA_STATE_PATH,
    AUDIT_REBUILD_PATH, AUTH_HEADER, AUTH_SCHEME, BOTTLES_PATH, FORGES_PATH, FORGES_REMOVE_PATH,
    HOST_CONFIG_PATH, HOST_CONTROL, JOBS_PATH, PAIRING_PATH, TRUST_PATH,
    LIVE_BOTTLES_PATH, ORCHESTRATOR_PATH, ROLE_ORCHESTRATOR, SESSIONS_PATH, STATUS_PATH,
    WORKSPACE_DISKS_PATH, WORKSPACE_DISKS_REMOVE_PATH,
    ARCHITECT_WARM_PATH, SUGGEST_PATH, LaunchRequest, Settings, status_body,
)
from ...lib import websocket as ws
from ...lib.log import debug
from .. import rotate_ca
from . import host_state, orchestrator, suggest_routes
from .attach import Viewer
from .audit_events import AuditUnavailable, digest_request
from .audit_import import AuditImportError
from .audit_index import IndexBreak
from .audit_journal import AuditRedactionError
from .bottles import BottleService
from .firehose import FirehoseHub
from .jobs import JobRegistry
from .live import LiveBottles
from .sessions import SessionManager

MAX_BODY_BYTES = 1 << 20
# Roles that may launch, list, tear down and watch bottles.
_BOTTLE_ROLES = (ROLE_CLI, ROLE_ORCHESTRATOR)
# The host DB's routes, `cli` only: the CLI never opens the file.
_HostRoute = Callable[[dict[str, object]], dict[str, object]]
_HOST_STATE: dict[str, _HostRoute] = {
    HOST_CONFIG_PATH: host_state.set_config, AUDIT_QUERY_PATH: host_state.audit_query,
    AUDIT_REBUILD_PATH: host_state.audit_rebuild, AUDIT_IMPORT_PATH: host_state.audit_import,
    FORGES_PATH: host_state.forge_connect, FORGES_REMOVE_PATH: host_state.forge_remove,
    TRUST_PATH: host_state.trust, PAIRING_PATH: host_state.set_pairing,
    AUDIT_PASSKEYS_PATH: host_state.trust_passkeys,
    BOTTLED_AGENTS_PATH: host_state.record_bottled_agent,
    BOTTLED_AGENTS_REMOVE_PATH: host_state.remove_bottled_agent,
    AGENT_PREFS_PATH: host_state.remember_agent,
    WORKSPACE_DISKS_REMOVE_PATH: host_state.remove_workspace_disk,
}
_HOST_STATE_READS: dict[str, _HostRoute] = {
    HOST_CONFIG_PATH: lambda _body: host_state.config(), FORGES_PATH: host_state.forges,
    TRUST_PATH: host_state.trusted, PAIRING_PATH: host_state.get_pairing,
    AUDIT_PASSKEYS_PATH: host_state.passkeys, BOTTLED_AGENTS_PATH: host_state.bottled_agents,
    AGENT_PREFS_PATH: host_state.agent_prefs, INFRA_STATE_PATH: host_state.infra_state,
    WORKSPACE_DISKS_PATH: host_state.workspace_disks,
}


class HostControlServer(ThreadingHTTPServer):
    """Serves the host controller's routes. `build_id` is reported on
    `/status`; `key` verifies `HOST_CONTROL` tokens. A guest listener passes
    `peers`, the `roles` it accepts and the `backend` it reports; listeners
    share `jobs`, `sessions`, `bottles` and `live`."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], build_id: str, key: str,
                 sessions: SessionManager | None = None,
                 bottles: BottleService | None = None, *,
                 jobs: JobRegistry | None = None, live: LiveBottles | None = None,
                 peers: str = "", roles: frozenset[str] = HOST_CONTROL.roles,
                 backend: str = "", firehose: FirehoseHub | None = None) -> None:
        if not key:
            raise ValueError("host controller needs its signing key")
        super().__init__(address, _Handler)
        self.build_id = build_id
        self.key = key
        self.peers = ipaddress.ip_network(peers) if peers else None
        self.roles = roles
        self.backend = backend
        self.jobs = jobs if jobs is not None else JobRegistry()
        self.sessions = sessions if sessions is not None else SessionManager({})
        self.bottles = bottles if bottles is not None else BottleService(self.sessions)
        self.live = live if live is not None else LiveBottles()
        self.firehose = firehose if firehose is not None else FirehoseHub()

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
        elif url.path == LIVE_BOTTLES_PATH:
            if self._allowed(_BOTTLE_ROLES):
                self._live(server, parse_qs(url.query))
        elif url.path == FIREHOSE_STREAM_PATH:
            self._firehose_stream(server, parse_qs(url.query))
        elif url.path in _HOST_STATE_READS:
            self._host_state(_HOST_STATE_READS[url.path], read=True)
        else:
            self._send(404, {"error": f"no route {url.path}"})

    def _live(self, server: HostControlServer, query: dict[str, list[str]]) -> None:
        """Running bottles: a guest listener's own backend, else the one asked
        for, else all."""
        backend = server.backend or query.get("backend", [""])[0]
        if backend and not server.live.known(backend):
            self._send(400, {"error": f"unknown backend {backend!r}"})
            return
        self._send(200, server.live.list(backend))

    def _attach(self, server: HostControlServer, session_id: str,
                query: dict[str, list[str]]) -> None:
        """Upgrade to a WebSocket and pump a read-only viewer of `session_id`."""
        if self._role() not in (ROLE_CLI, ROLE_ORCHESTRATOR):
            self._reject(self._role())
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
                                lambda: server.bottles.teardown(bottle_id, role))
        self._send(202, {"job": job.id})

    def _launch(self, server: HostControlServer) -> None:
        role = self._allowed(_BOTTLE_ROLES)
        if role is None:
            return
        try:
            request = LaunchRequest.from_json(self._json_body(), role)
        except PermissionError as e:
            self._reject(role)
            self._send(403, {"error": str(e)})
            return
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        job = server.jobs.start("bottle.launch", role,
                                lambda: server.bottles.launch(request, role))
        self._send(202, {"job": job.id})

    def _suggest(self, server: HostControlServer) -> None:
        """A bottle suggestion for the CLI, run here so the architect's warm
        bottle serves it (PRD 0117)."""
        role = self._allowed((ROLE_CLI,))
        if role is None:
            return
        try:
            body = self._json_body()
            task, rules = body.get("task"), body.get("rules", False)
            if not isinstance(task, str) or not task.strip():
                raise ValueError("task must be a non-empty string")
            if not isinstance(rules, bool):
                raise ValueError("rules must be true or false")
            settings = Settings.from_json(body.get("settings"))
            cwd = suggest_routes.cwd_of(body.get("cwd"))
            # pylint: disable-next=import-outside-toplevel
            from ...suggest.turns import parse_turns
            turns = parse_turns(body.get("turns"))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        job = server.jobs.start("bottle.suggest", role,
                                lambda: suggest_routes.suggest(task, cwd, rules, settings, turns))
        self._send(202, {"job": job.id})

    def _architect_warm(self, server: HostControlServer) -> None:
        """Boot the architect's bottle in the background, if it's down."""
        del server
        if self._allowed((ROLE_CLI,)) is None:
            return
        try:
            settings = Settings.from_json(self._json_body().get("settings"))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        self._send(202, suggest_routes.warm(settings))

    def _audit_append(self, server: HostControlServer) -> None:
        """Write one event the orchestrator relays, its own or the gateway's."""
        if self._allowed((ROLE_ORCHESTRATOR,)) is None:
            return
        try:
            body = self._json_body()
            producer = body.get("producer")
            if producer not in AUDIT_PRODUCERS:
                raise ValueError(f"producer must be one of {sorted(AUDIT_PRODUCERS)}")
            raw = body.get("event")
            if not isinstance(raw, dict):
                raise ValueError("event must be an object")
            event = AuditEvent.from_json(cast(dict[str, object], raw))
        except (ValueError, TypeError) as e:
            self._send(400, {"error": str(e)})
            return
        try:
            written = server.bottles.audit.append(event, producer=str(producer))
        except AuditRedactionError as e:
            self._send(422, {"error": str(e)})
            return
        except AuditUnavailable as e:
            self._send(503, {"error": str(e)})
            return
        self._send(200, {"written": written})

    def _host_post(self, server: HostControlServer) -> None:
        del server
        self._host_state(_HOST_STATE[urlsplit(self.path).path])

    def _host_state(self, handler: _HostRoute, *, read: bool = False) -> None:
        """A host DB route (`host_state`). A journal that won't index or a
        segment that won't import is 409."""
        if self._allowed((ROLE_CLI,)) is None:
            return
        try:
            reply = handler({} if read else self._json_body())
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        except (IndexBreak, AuditImportError) as e:
            self._send(409, {"error": str(e)})
            return
        except (OSError, sqlite3.Error) as e:
            self._send(500, {"error": str(e)})
            return
        self._send(200, reply)

    def _firehose_publish(self, server: HostControlServer) -> None:
        """A batch of request records the orchestrator relays from the gateway."""
        if self._allowed((ROLE_ORCHESTRATOR,)) is None:
            return
        try:
            body = self._json_body()
            records = body.get("records")
            dropped = body.get("dropped", 0)
            if not isinstance(records, list) or not isinstance(dropped, int):
                raise ValueError("expected records (a list) and dropped (an int)")
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        batch = [r for r in records if isinstance(r, dict)]
        self._send(200, server.firehose.publish(batch, dropped).to_json())

    def _firehose_stream(self, server: HostControlServer, query: dict[str, list[str]]) -> None:
        """`audit tail`: each request record as it arrives, as NDJSON."""
        if self._allowed((ROLE_CLI,)) is None:
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        records = server.firehose.subscribe(bottle=(query.get("bottle") or [""])[0],
                                            host=(query.get("host") or [""])[0])
        try:
            for record in records:
                # A blank line every quiet second notices a client that left.
                self.wfile.write(b"\n" if record is None else json.dumps(record).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            records.close()
        self.close_connection = True

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        server = cast(HostControlServer, self.server)
        path = urlsplit(self.path).path
        route = {BOTTLES_PATH: self._launch, AUDIT_APPEND_PATH: self._audit_append,
                 FIREHOSE_PATH: self._firehose_publish, SUGGEST_PATH: self._suggest,
                 ARCHITECT_WARM_PATH: self._architect_warm,
                 **dict.fromkeys(_HOST_STATE, self._host_post)}.get(path)
        if route is not None:
            route(server)
            return
        action = path.removeprefix(ORCHESTRATOR_PATH + "/")
        if not path.startswith(ORCHESTRATOR_PATH + "/") or \
                action not in ("ensure", "restart", "status", "rotate-ca"):
            self._send(404, {"error": f"no route {path}"})
            return
        role = self._role()
        if role is None:
            self._reject(role)
            self._send(401, {"error": "missing or invalid token"})
            return
        if role != ROLE_CLI:
            self._reject(role)
            self._send(403, {"error": f"{path} is for the cli role"})
            return
        try:
            body = self._json_body()
            settings = Settings.from_json(body.get("settings"))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        allow = body.get("allow_reattach_failures") is True
        if action == "status":
            self._send(200, orchestrator.status(settings))
            return
        work = partial(rotate_ca.rotate, settings) if action == "rotate-ca" else \
            partial(orchestrator.ensure, settings, restart=action == "restart",
                    allow_reattach_failures=allow)
        job = server.jobs.start(f"orchestrator.{action}", role, work)
        self._send(202, {"job": job.id})

    def _json_body(self) -> dict[str, object]:
        """The JSON object body; ValueError if missing, too big or not an
        object."""
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
            self._reject(role)
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
        """The caller's role if in `roles`; otherwise send 401/403 and return
        None."""
        role = self._role()
        if role is None:
            self._reject(role)
            self._send(401, {"error": "missing or invalid token"})
        elif role not in roles:
            self._reject(role)
            self._send(403, {"error": f"not for the {role} role"})
            return None
        return role

    def _reject(self, role: str | None) -> None:
        """Record a call refused for its token or role."""
        op = f"{self.command} {urlsplit(self.path).path}"
        cast(HostControlServer, self.server).bottles.audit.rejected(
            op, role=role, request_digest=digest_request({"op": op}))

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
