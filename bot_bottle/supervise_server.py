"""Supervise sidecar HTTP server (PRD 0013).

Per-bottle MCP server exposing two tools — `egress-block`,
`capability-block` — that the agent calls to propose config changes
when stuck. Each tool call:

  1. Validates the proposed file syntactically.
  2. Writes a Proposal to /run/supervise/queue/ (bind-mounted from
     the host's ~/.bot-bottle/queue/<slug>/).
  3. Blocks polling for a matching Response file.
  4. Returns the operator's `{status, notes}` to the agent.

The bottle slug arrives via SUPERVISE_BOTTLE_SLUG env (stamped at
container creation by the backend's start step). The queue dir comes
from SUPERVISE_QUEUE_DIR (default `/run/supervise/queue`).

Speaks MCP over HTTP+JSON-RPC. Methods handled:

  * `initialize`           — handshake; returns server info + caps.
  * `notifications/initialized` — ack-only.
  * `tools/list`           — returns the tool definitions.
  * `tools/call`           — validates, queues, blocks, returns.

Everything else returns JSON-RPC error -32601 (method not found).

Stdlib-only. The Dockerfile copies this file + bot_bottle/supervise.py
into the image; the server imports `supervise` for the queue / Proposal
plumbing.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Same-directory import inside the bundle container; `supervise.py`
# is COPYed alongside this file by Dockerfile.sidecars.
import supervise as _sv


# --- JSON-RPC / MCP plumbing ----------------------------------------------


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bot-bottle-supervise"
SERVER_VERSION = "0.1.0"

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 standard error codes.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30.0
MIN_RESPONSE_POLL_INTERVAL_SECONDS = 0.05
EGRESS_LIST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: dict[str, object]
    id: object  # None for notifications; int/str/null for requests
    is_notification: bool


def parse_jsonrpc(body: bytes) -> JsonRpcRequest:
    """Parse a single JSON-RPC 2.0 request body. Raises ValueError
    with a JSON-RPC error code attached if the shape is wrong."""
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        raise _RpcError(ERR_PARSE, f"parse error: {e}") from e
    if not isinstance(raw, dict):
        raise _RpcError(ERR_INVALID_REQUEST, "request must be a JSON object")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise _RpcError(ERR_INVALID_REQUEST, "jsonrpc field must be '2.0'")
    method = raw.get("method")
    if not isinstance(method, str):
        raise _RpcError(ERR_INVALID_REQUEST, "method must be a string")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _RpcError(ERR_INVALID_PARAMS, "params must be an object")
    rpc_id = raw.get("id", _NO_ID)
    is_notification = rpc_id is _NO_ID
    return JsonRpcRequest(
        method=method,
        params=params,
        id=None if is_notification else rpc_id,
        is_notification=is_notification,
    )


_NO_ID = object()


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def jsonrpc_result(request_id: object, result: object) -> bytes:
    payload = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
    return (json.dumps(payload) + "\n").encode("utf-8")


def jsonrpc_error(request_id: object, code: int, message: str) -> bytes:
    payload = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    return (json.dumps(payload) + "\n").encode("utf-8")


# --- Tool definitions ------------------------------------------------------


TOOL_DEFINITIONS: list[dict[str, object]] = [
    {
        "name": _sv.TOOL_EGRESS_BLOCK,
        "description": (
            "Call when egress refused your HTTPS request — host "
            "without a matching route, or a path outside the route's "
            "path_allowlist (typically a 403 from the proxy). Propose "
            "a SINGLE route to add: the host you need + (optionally) "
            "a path_allowlist + (optionally) an auth block. The "
            "supervisor merges the route into the live table at "
            "approval time — you do NOT need to see or reproduce the "
            "existing routes, and you do not pass a full routes file. "
            "If the host already has a route, the proposed "
            "path_allowlist entries are unioned with the existing "
            "ones (host stays single-route). The operator approves "
            "or rejects in the supervise TUI. On approval the "
            "supervisor writes the merged routes.yaml, SIGHUPs "
            "egress (atomic swap, no dropped connections), and "
            "writes the merged routes.yaml and SIGHUPs egress "
            "(atomic swap, no dropped connections)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": (
                        "The hostname to allow (e.g. 'api.github.com'). "
                        "Case-insensitive on match."
                    ),
                },
                "path_allowlist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional URL path prefixes the route permits. "
                        "Each must start with '/'. Omit to allow all "
                        "paths under this host (bare-pass route)."
                    ),
                },
                "auth": {
                    "type": "object",
                    "description": (
                        "Optional credential injection. {scheme, "
                        "token_ref}: scheme is 'Bearer' or 'token'; "
                        "token_ref names the host env var holding the "
                        "secret value. Omit to add a host without "
                        "credential injection. Ignored if the host "
                        "already has a route (operator decides auth "
                        "changes, not the agent)."
                    ),
                    "properties": {
                        "scheme": {"type": "string"},
                        "token_ref": {"type": "string"},
                    },
                    "required": ["scheme", "token_ref"],
                    "additionalProperties": False,
                },
                "justification": {
                    "type": "string",
                    "description": "Why this host needs to be allowed.",
                },
            },
            "required": ["host", "justification"],
        },
    },
    {
        "name": _sv.TOOL_LIST_EGRESS_ROUTES,
        "description": (
            "List the current egress route table — the bottle's "
            "allowlist. Returns JSON with one entry per allowed host, "
            "each carrying its path_allowlist (if any) and whether "
            "the proxy injects Authorization for the route. Use this "
            "before composing an `egress-block` proposal so the new "
            "routes file extends the live one rather than replacing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_CAPABILITY_BLOCK,
        "description": (
            "Call when the bottle is missing a tool, skill, permission, "
            "or env var you need — something that lives in the agent "
            "Dockerfile rather than in the egress routes. "
            "Read the current Dockerfile from "
            "/etc/bot-bottle/current-config/Dockerfile, compose a "
            "modified version, and pass the full new file plus a "
            "justification. On approval the supervisor rebuilds the "
            "bottle from the new Dockerfile and starts a replacement on "
            "the same branch (wired in PRD 0016; v1 acknowledges only)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dockerfile": {
                    "type": "string",
                    "description": "Full proposed Dockerfile content.",
                },
                "justification": {
                    "type": "string",
                    "description": "Why this capability is needed.",
                },
            },
            "required": ["dockerfile", "justification"],
        },
    },
]


# Map each non-egress tool to the input field that carries the agent's
# payload (stored in Proposal.proposed_file). egress-block builds its
# payload from structured input fields in `handle_egress_block`.
PROPOSED_FILE_FIELD: dict[str, str] = {
    _sv.TOOL_CAPABILITY_BLOCK: "dockerfile",
}


# --- Validation ------------------------------------------------------------


# Auth schemes accepted on egress-block proposals — match the
# manifest-side EGRESS_AUTH_SCHEMES.
_AUTH_SCHEMES = ("Bearer", "token")


def validate_proposed_file(tool: str, content: str) -> None:
    """Syntactic validation. The operator is the real gate; this just
    catches obvious paste-errors / wrong-tool selections before they
    enter the queue."""
    if not content.strip():
        raise _RpcError(ERR_INVALID_PARAMS, f"{tool}: proposed file is empty")
    if tool == _sv.TOOL_CAPABILITY_BLOCK:
        # Dockerfiles are too varied to validate syntactically beyond
        # non-empty. The operator reads the diff in the TUI.
        pass
    else:
        raise _RpcError(ERR_INVALID_PARAMS, f"unknown tool {tool!r}")


def _validate_and_bundle_egress_route(
    args: dict[str, object],
) -> str:
    """Validate egress-block input fields and bundle them into
    a JSON string that becomes the Proposal.proposed_file. Raises
    _RpcError on bad input — the agent retries with a fixed shape."""
    tool = _sv.TOOL_EGRESS_BLOCK
    host = args.get("host")
    if not isinstance(host, str) or not host.strip():
        raise _RpcError(
            ERR_INVALID_PARAMS,
            f"{tool}: 'host' is required and must be a non-empty string",
        )
    payload: dict[str, object] = {"host": host}

    path_allow_raw = args.get("path_allowlist")
    if path_allow_raw is not None:
        if not isinstance(path_allow_raw, list):
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: 'path_allowlist' must be an array of strings",
            )
        prefixes: list[str] = []
        for i, p in enumerate(path_allow_raw):
            if not isinstance(p, str):
                raise _RpcError(
                    ERR_INVALID_PARAMS,
                    f"{tool}: path_allowlist[{i}] must be a string",
                )
            if not p.startswith("/"):
                raise _RpcError(
                    ERR_INVALID_PARAMS,
                    f"{tool}: path_allowlist[{i}] {p!r} must start with '/'",
                )
            prefixes.append(p)
        if prefixes:
            payload["path_allowlist"] = prefixes

    auth_raw = args.get("auth")
    if auth_raw is not None:
        if not isinstance(auth_raw, dict):
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: 'auth' must be an object with 'scheme' and 'token_ref'",
            )
        scheme = auth_raw.get("scheme")
        token_ref = auth_raw.get("token_ref")
        if not isinstance(scheme, str) or scheme not in _AUTH_SCHEMES:
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: auth.scheme must be one of "
                f"{', '.join(_AUTH_SCHEMES)} (got {scheme!r})",
            )
        if not isinstance(token_ref, str) or not token_ref:
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: auth.token_ref must be a non-empty string "
                f"naming the host env var holding the token",
            )
        payload["auth"] = {"scheme": scheme, "token_ref": token_ref}

    return json.dumps(payload, indent=2) + "\n"


# --- MCP handlers ----------------------------------------------------------


@dataclass(frozen=True)
class ServerConfig:
    bottle_slug: str
    queue_dir: Path
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS


def handle_initialize(_params: dict[str, object]) -> dict[str, object]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(_params: dict[str, object]) -> dict[str, object]:
    return {"tools": TOOL_DEFINITIONS}


def handle_list_egress_routes(
    _params: dict[str, object],
    _config: ServerConfig,
) -> dict[str, object]:
    """Fetch the live egress route table via its
    `_egress.local/allowlist` introspection endpoint. The
    request goes through egress as a forward proxy; the
    addon recognises the magic host and synthesizes a response —
    no real upstream connection, no allowlist enforcement
    against the magic host. Returns the JSON payload as the
    tool's text content."""
    proxy_handler = urllib.request.ProxyHandler({
        "http": _sv.EGRESS_FORWARD_PROXY,
    })
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(_sv.EGRESS_INTROSPECT_URL, timeout=EGRESS_LIST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"list-egress-routes: could not reach "
                    f"{_sv.EGRESS_INTROSPECT_URL!r} via "
                    f"{_sv.EGRESS_FORWARD_PROXY!r}: {e}"
                ),
            }],
            "isError": True,
        }
    return {
        "content": [{"type": "text", "text": body}],
        "isError": False,
    }


def handle_tools_call(
    params: dict[str, object],
    config: ServerConfig,
) -> dict[str, object]:
    """Validates the proposal, writes it to the queue, blocks waiting
    for a Response, returns the result wrapped in MCP `content`.

    Side-effect-free `list-*` tools short-circuit before the queue/
    blocking machinery — they're read-only introspection that
    doesn't need operator approval."""
    name = params.get("name")
    if not isinstance(name, str):
        raise _RpcError(ERR_INVALID_PARAMS, "tools/call missing 'name'")
    if name == _sv.TOOL_LIST_EGRESS_ROUTES:
        return handle_list_egress_routes(typing.cast(dict[str, object], params.get("arguments", {})), config)

    args_raw = params.get("arguments", {})
    if not isinstance(args_raw, dict):
        raise _RpcError(ERR_INVALID_PARAMS, "tools/call 'arguments' must be an object")

    justification = args_raw.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        raise _RpcError(
            ERR_INVALID_PARAMS,
            f"{name}: 'justification' is required and must be a non-empty string",
        )

    if name == _sv.TOOL_EGRESS_BLOCK:
        # Structured input → JSON bundle on Proposal.proposed_file.
        # The dashboard's apply step (egress_apply.add_route)
        # parses this JSON, fetches the current routes, merges in
        # the new one, and writes the merged file.
        proposed_file = _validate_and_bundle_egress_route(args_raw)
    elif name in PROPOSED_FILE_FIELD:
        file_field = PROPOSED_FILE_FIELD[name]
        proposed_file = args_raw.get(file_field)
        if not isinstance(proposed_file, str):
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{name}: '{file_field}' is required and must be a string",
            )
        validate_proposed_file(name, proposed_file)
    else:
        raise _RpcError(ERR_INVALID_PARAMS, f"unknown tool {name!r}")

    proposal = _sv.Proposal.new(
        bottle_slug=config.bottle_slug,
        tool=name,
        proposed_file=proposed_file,
        justification=justification,
        current_file_hash=_sv.sha256_hex(proposed_file),
    )
    _sv.write_proposal(config.queue_dir, proposal)
    sys.stderr.write(
        f"supervise: queued proposal {proposal.id} ({name}) "
        f"for bottle {config.bottle_slug}; waiting for operator...\n"
    )
    sys.stderr.flush()
    deadline = time.monotonic() + config.response_timeout_seconds
    try:
        response = _sv.wait_for_response(
            config.queue_dir,
            proposal.id,
            poll_interval=MIN_RESPONSE_POLL_INTERVAL_SECONDS,
            deadline=deadline,
        )
    except TimeoutError:
        text = format_pending_response_text(config.response_timeout_seconds)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
    _sv.archive_proposal(config.queue_dir, proposal.id)

    text = format_response_text(response)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": response.status == _sv.STATUS_REJECTED,
    }


def format_response_text(response: "_sv.Response") -> str:
    """Pretty-print a Response for the tool's text content. The agent
    reads the text and decides whether to retry / give up / surface."""
    lines = [f"status: {response.status}"]
    if response.notes:
        lines.append(f"notes: {response.notes}")
    if response.status == _sv.STATUS_MODIFIED and response.final_file is not None:
        lines.append("the operator modified your proposal before approving; "
                     "the final config is now what's been applied")
    return "\n".join(lines)


def format_pending_response_text(timeout_seconds: float) -> str:
    return "\n".join([
        "status: pending",
        (
            "notes: operator response timed out after "
            f"{timeout_seconds:g}s; proposal remains queued"
        ),
    ])


# --- HTTP transport --------------------------------------------------------


# Max request body the server accepts. Generous because Dockerfile
# proposals can be a few KB; routes.json is small. 1 MB is well above
# any realistic config file.
MAX_BODY_BYTES = 1 * 1024 * 1024


class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Per-request JSON-RPC handler. Each tools/call may block for
    a long time; the ThreadingMixIn on the server class ensures
    other requests can be processed concurrently."""

    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, format: str, *args: typing.Any) -> None:  # noqa: A002
        if os.environ.get("SUPERVISE_DEBUG"):
            super().log_message(format, *args)

    def do_GET(self) -> None:
        # /health for liveness; everything else 405. POST is the only
        # method MCP needs.
        if self.path == "/health":
            self._write_text(200, "ok\n")
            return
        self._write_text(405, "use POST for MCP requests\n")

    def do_POST(self) -> None:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._write_text(411, "Content-Length required\n")
            return
        try:
            length = int(length_header)
        except ValueError:
            self._write_text(400, "invalid Content-Length\n")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._write_text(413, "request body too large\n")
            return
        body = self.rfile.read(length)

        try:
            req = parse_jsonrpc(body)
        except _RpcError as e:
            self._write_jsonrpc(jsonrpc_error(None, e.code, e.message))
            return

        config = typing.cast("MCPServer", self.server).config

        try:
            result = self._dispatch(req, config)
        except _RpcError as e:
            self._write_jsonrpc(jsonrpc_error(req.id, e.code, e.message))
            return
        except Exception as e:  # noqa: W0718 — catch-all for RPC dispatch errors
            sys.stderr.write(f"supervise: internal error: {e}\n")
            self._write_jsonrpc(jsonrpc_error(req.id, ERR_INTERNAL, "internal error"))
            return

        if req.is_notification:
            self._write_text(202, "")
            return
        self._write_jsonrpc(jsonrpc_result(req.id, result))

    def _dispatch(self, req: JsonRpcRequest, config: ServerConfig) -> object:
        method = req.method
        if method == "initialize":
            return handle_initialize(req.params)
        if method == "notifications/initialized":
            return None  # ack-only
        if method == "tools/list":
            return handle_tools_list(req.params)
        if method == "tools/call":
            return handle_tools_call(req.params, config)
        raise _RpcError(ERR_METHOD_NOT_FOUND, f"method not found: {method}")

    def _write_jsonrpc(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _write_text(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)


class MCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    config: ServerConfig = ServerConfig(bottle_slug="", queue_dir=Path())


# --- Entry point -----------------------------------------------------------


def serve(
    *,
    bottle_slug: str,
    queue_dir: Path,
    port: int = _sv.SUPERVISE_PORT,
    bind: str = "0.0.0.0",
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
) -> typing.NoReturn:
    queue_dir.mkdir(parents=True, exist_ok=True)
    server = MCPServer((bind, port), MCPHandler)
    server.config = ServerConfig(
        bottle_slug=bottle_slug,
        queue_dir=queue_dir,
        response_timeout_seconds=response_timeout_seconds,
    )
    sys.stderr.write(
        f"supervise listening on {bind}:{port}; "
        f"slug={bottle_slug!r}; queue={queue_dir}; "
        f"tools: {', '.join(t['name'] for t in TOOL_DEFINITIONS)}\n"  # type: ignore[arg-type]
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    sys.exit(0)


def main(argv: list[str]) -> int:
    del argv  # config is env-only, no CLI flags
    bottle_slug = os.environ.get("SUPERVISE_BOTTLE_SLUG", "")
    if not bottle_slug:
        sys.stderr.write("supervise: SUPERVISE_BOTTLE_SLUG env is unset\n")
        return 2
    queue_dir = Path(os.environ.get("SUPERVISE_QUEUE_DIR", _sv.QUEUE_DIR_IN_CONTAINER))
    port = int(os.environ.get("SUPERVISE_PORT", str(_sv.SUPERVISE_PORT)))
    bind = os.environ.get("SUPERVISE_BIND", "0.0.0.0")
    try:
        response_timeout_seconds = _response_timeout_from_env(os.environ)
    except ValueError as e:
        sys.stderr.write(f"supervise: {e}\n")
        return 2
    serve(
        bottle_slug=bottle_slug,
        queue_dir=queue_dir,
        port=port,
        bind=bind,
        response_timeout_seconds=response_timeout_seconds,
    )
    return 0  # serve() does not return


def _response_timeout_from_env(env: typing.Mapping[str, str]) -> float:
    raw = env.get("SUPERVISE_RESPONSE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_RESPONSE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as e:
        raise ValueError(
            "SUPERVISE_RESPONSE_TIMEOUT_SECONDS must be a positive number"
        ) from e
    if value <= 0:
        raise ValueError(
            "SUPERVISE_RESPONSE_TIMEOUT_SECONDS must be a positive number"
        )
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
