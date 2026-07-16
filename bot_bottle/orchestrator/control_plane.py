"""Orchestrator HTTP control plane (PRD 0070).

The backend-agnostic control-plane RPC (CLI / console -> orchestrator) over
**HTTP** — the universal transport chosen in 0070 (works on every host; no
vsock / unix-socket portability caveats):

    GET    /health                    -> 200 {"status": "ok"}
    GET    /gateway                   -> 200 {"configured", ["name","running"]}
    GET    /bottles                   -> 200 {"bottles": [ <redacted record>, ...]}
    POST   /bottles                   -> 201 {"bottle_id","identity_token"}  (launch)
                                         body: {"source_ip", ["image_ref"],
                                                ["metadata"], ["policy"]}
    PUT    /bottles/<bottle_id>/policy -> 200 {"updated": true} | 404  (live reload)
                                         body: {"policy"}
    DELETE /bottles/<bottle_id>        -> 200 {"torn_down": true} | 404  (teardown)
    POST   /attribute                 -> 200 {"bottle_id"} | 403
    POST   /resolve                   -> 200 {"bottle_id","policy"} | 403
                                         body: {"source_ip","identity_token"}

`POST /bottles` / `DELETE` drive the full launch lifecycle: they mint (or
tear down) the bottle in the registry AND broker the backend-native launch
via the orchestrator. Register/deregister without a launch are internal to
`Orchestrator`, not exposed here.

Routing/handling is the pure function `dispatch()` so it is unit-testable
without a socket; `Handler` / `ControlPlaneServer` / `make_server` are a
thin stdlib adapter around it. Listing redacts identity tokens — they are
returned only once, to the caller that launches the bottle.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import typing
from urllib.parse import urlsplit

from .service import Orchestrator

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


def dispatch(  # pylint: disable=too-many-return-statements,too-many-branches
    orch: Orchestrator, method: str, path: str, body: bytes
) -> tuple[int, Json]:
    """Route one control-plane request to a (status, payload) pair. Pure —
    no I/O beyond the orchestrator — so it is fully testable without a socket."""
    route = urlsplit(path).path.rstrip("/") or "/"

    if method == "GET" and route == "/health":
        return 200, {"status": "ok"}

    if method == "GET" and route == "/gateway":
        return 200, orch.gateway_status()

    if method == "GET" and route == "/bottles":
        return 200, {"bottles": [r.redacted() for r in orch.registry.all()]}

    if method == "POST" and route == "/bottles":
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        image_ref = data.get("image_ref")
        metadata = data.get("metadata")
        policy = data.get("policy")
        raw_tokens = data.get("tokens")
        tokens = {
            k: v for k, v in raw_tokens.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw_tokens, dict) else {}
        rec = orch.launch_bottle(
            source_ip,
            image_ref=image_ref if isinstance(image_ref, str) else "",
            metadata=metadata if isinstance(metadata, str) else "",
            policy=policy if isinstance(policy, str) else "",
            tokens=tokens,
        )
        return 201, {"bottle_id": rec.bottle_id, "identity_token": rec.identity_token}

    if method == "PUT" and route.startswith("/bottles/") and route.endswith("/policy"):
        bottle_id = route[len("/bottles/"):-len("/policy")]
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        policy = data.get("policy")
        if not isinstance(policy, str):
            return 400, {"error": "policy (string) is required"}
        if orch.set_policy(bottle_id, policy):
            return 200, {"updated": True}
        return 404, {"error": "no such bottle"}

    if method == "DELETE" and route.startswith("/bottles/"):
        bottle_id = route[len("/bottles/"):]
        if orch.teardown_bottle(bottle_id):
            return 200, {"torn_down": True}
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
        rec = orch.attribute(source_ip, token)
        if rec is None:
            return 403, {"error": "unattributed"}
        return 200, {"bottle_id": rec.bottle_id}

    if method == "POST" and route == "/resolve":
        # The per-request lookup the multi-tenant gateway makes: returns the
        # bottle's policy. Requires a matching (source_ip, identity_token)
        # pair — a missing/empty/mismatched token fail-closes (403), no
        # source-IP-only fallback.
        try:
            data = _parse_json_object(body)
        except ValueError as e:
            return 400, {"error": f"invalid JSON: {e}"}
        source_ip = data.get("source_ip")
        token = data.get("identity_token")
        if not isinstance(source_ip, str) or not source_ip:
            return 400, {"error": "source_ip (string) is required"}
        rec = orch.resolve(source_ip, token if isinstance(token, str) else "")
        if rec is None:
            return 403, {"error": "unattributed"}
        # tokens are the in-memory per-bottle egress auth values the gateway
        # injects; served here, never persisted.
        return 200, {
            "bottle_id": rec.bottle_id,
            "policy": rec.policy,
            "tokens": orch.tokens_for(rec.bottle_id),
        }

    return 404, {"error": "not found"}


class Handler(http.server.BaseHTTPRequestHandler):
    """Thin stdlib adapter: read the body, call `dispatch`, write JSON."""

    # Quiet by default (the orchestrator has its own logging); opt back into
    # stdlib access logging with BOT_BOTTLE_ORCHESTRATOR_DEBUG.
    def log_message(self, format: str, *args: typing.Any) -> None:  # noqa: A002
        if os.environ.get("BOT_BOTTLE_ORCHESTRATOR_DEBUG"):
            super().log_message(format, *args)

    def _serve(self, method: str) -> None:
        """Read the request body, dispatch it, and write the JSON reply. A
        dispatch failure (e.g. a broker error) returns a 500 rather than
        crashing the connection, so one bad request can't take the control
        plane down for the caller."""
        server = self.server
        assert isinstance(server, ControlPlaneServer)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        try:
            status, payload = dispatch(server.orchestrator, method, self.path, body)
        except Exception as e:  # noqa: BLE001 — the control plane must stay up
            sys.stderr.write(f"orchestrator: {method} {self.path} failed: {e!r}\n")
            sys.stderr.flush()
            status, payload = 500, {"error": f"internal error: {e}"}
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

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_DELETE(self) -> None:
        self._serve("DELETE")


class ControlPlaneServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threading HTTP server that carries the orchestrator for its handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator
        super().__init__(address, Handler)


def make_server(
    orchestrator: Orchestrator, host: str = "127.0.0.1", port: int = 0
) -> ControlPlaneServer:
    """Build (but do not start) a control-plane server. `port=0` binds an
    ephemeral port — read `server.server_address` for the actual one."""
    return ControlPlaneServer((host, port), orchestrator)


__all__ = ["dispatch", "Handler", "ControlPlaneServer", "make_server", "Json"]
