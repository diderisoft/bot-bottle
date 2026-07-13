"""Orchestrator HTTP control plane (PRD 0070).

The backend-agnostic control-plane RPC (CLI / console -> orchestrator) over
**HTTP** — the universal transport chosen in 0070 (works on every host; no
vsock / unix-socket portability caveats). Endpoints mutate the live
registry, so register/deregister are the *live-reload* path — no restart:

    GET    /health              -> 200 {"status": "ok"}
    GET    /bottles             -> 200 {"bottles": [ <redacted record>, ... ]}
    POST   /bottles             -> 201 {"bottle_id", "identity_token"}
                                   body: {"source_ip", ["bottle_id"], ["metadata"]}
    DELETE /bottles/<bottle_id> -> 200 {"deregistered": true} | 404
    POST   /attribute           -> 200 {"bottle_id"} | 403
                                   body: {"source_ip", "identity_token"}

Routing/handling is the pure function `dispatch()` so it is unit-testable
without a socket; `Handler` / `ControlPlaneServer` / `make_server` are a
thin stdlib adapter around it.

Note the listing redacts identity tokens — they are never returned except
once, to the caller that registers the bottle.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import typing
from urllib.parse import urlsplit

from .registry import RegistryStore

# JSON body payload type (parsed request / rendered response).
Json = dict[str, object]


def _parse_json_object(body: bytes) -> Json:
    """Parse a JSON object body. Raises ValueError for non-objects / bad JSON."""
    if not body:
        return {}
    obj = json.loads(body)  # raises json.JSONDecodeError (a ValueError)
    if not isinstance(obj, dict):
        raise ValueError("request body must be a JSON object")
    return obj


def dispatch(  # pylint: disable=too-many-return-statements
    registry: RegistryStore, method: str, path: str, body: bytes
) -> tuple[int, Json]:
    """Route one control-plane request to a (status, payload) pair. Pure —
    no I/O beyond the registry — so it is fully testable without a socket."""
    route = urlsplit(path).path.rstrip("/") or "/"

    if method == "GET" and route == "/health":
        return 200, {"status": "ok"}

    if method == "GET" and route == "/bottles":
        return 200, {"bottles": [r.redacted() for r in registry.all()]}

    if method == "POST" and route == "/bottles":
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        bottle_id = data.get("bottle_id")
        metadata = data.get("metadata")
        rec = registry.register(
            source_ip,
            bottle_id=bottle_id if isinstance(bottle_id, str) else None,
            metadata=metadata if isinstance(metadata, str) else "",
        )
        return 201, {"bottle_id": rec.bottle_id, "identity_token": rec.identity_token}

    if method == "DELETE" and route.startswith("/bottles/"):
        bottle_id = route[len("/bottles/"):]
        if registry.deregister(bottle_id):
            return 200, {"deregistered": True}
        return 404, {"error": "no such bottle"}

    if method == "POST" and route == "/attribute":
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        if not isinstance(source_ip, str) or not isinstance(token, str):
            return 400, {"error": "source_ip and identity_token (strings) required"}
        rec = registry.attribute(source_ip, token)
        if rec is None:
            return 403, {"error": "unattributed"}
        return 200, {"bottle_id": rec.bottle_id}

    return 404, {"error": "not found"}


class Handler(http.server.BaseHTTPRequestHandler):
    """Thin stdlib adapter: read the body, call `dispatch`, write JSON."""

    # Quiet by default (the orchestrator has its own logging); opt back into
    # stdlib access logging with BOT_BOTTLE_ORCHESTRATOR_DEBUG.
    def log_message(self, format: str, *args: typing.Any) -> None:  # noqa: A002
        if os.environ.get("BOT_BOTTLE_ORCHESTRATOR_DEBUG"):
            super().log_message(format, *args)

    def _serve(self, method: str) -> None:
        """Read the request body, dispatch it, and write the JSON reply."""
        server = self.server
        assert isinstance(server, ControlPlaneServer)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        status, payload = dispatch(server.registry, method, self.path, body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_DELETE(self) -> None:
        self._serve("DELETE")


class ControlPlaneServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server that carries the registry for its handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], registry: RegistryStore) -> None:
        self.registry = registry
        super().__init__(address, Handler)


def make_server(
    registry: RegistryStore, host: str = "127.0.0.1", port: int = 0
) -> ControlPlaneServer:
    """Build (but do not start) a control-plane server. `port=0` binds an
    ephemeral port — read `server.server_address` for the actual one."""
    return ControlPlaneServer((host, port), registry)


__all__ = ["dispatch", "Handler", "ControlPlaneServer", "make_server", "Json"]
