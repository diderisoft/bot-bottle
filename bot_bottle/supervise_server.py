"""Supervise sidecar HTTP server (PRD 0013).

Per-bottle MCP server exposing tools the agent calls to propose egress
config changes when stuck. The tools are `egress-allow`,
`egress-block`, and `list-egress-routes`.

Each queued tool call:

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
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    # Same-directory imports inside the bundle container; these files are
    # COPYed flat under /app by Dockerfile.sidecars.
    from egress_addon_core import LOG_OFF, load_config
    import supervise as _sv
except ModuleNotFoundError:
    # Package imports for host-side tests and tooling.
    from .egress_addon_core import LOG_OFF, load_config
    from . import supervise as _sv


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
        raise _RpcClientError(ERR_PARSE, f"parse error: {e}") from e
    if not isinstance(raw, dict):
        raise _RpcClientError(ERR_INVALID_REQUEST, "request must be a JSON object")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise _RpcClientError(ERR_INVALID_REQUEST, "jsonrpc field must be '2.0'")
    method = raw.get("method")
    if not isinstance(method, str):
        raise _RpcClientError(ERR_INVALID_REQUEST, "method must be a string")
    params = raw.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _RpcClientError(ERR_INVALID_PARAMS, "params must be an object")
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
    """Base class for all typed RPC errors that surface as JSON-RPC error responses."""
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _RpcClientError(_RpcError):
    """Caller sent a bad request; returned verbatim, no server-side logging."""


class _RpcInternalError(_RpcError):
    """Server-side fault; logged at ERROR with cause, always returns ERR_INTERNAL."""
    def __init__(self, message: str) -> None:
        super().__init__(ERR_INTERNAL, message)


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


# Shared by both proposal tools (egress-allow / egress-block): they take the
# same arguments and differ only in their top-level tool description. Kept as a
# single source of truth so the schema can't drift between the two tools.
_ROUTES_YAML_DESCRIPTION = (
    "Full proposed /etc/egress/routes.yaml content. "
    "Each route entry accepts these keys:\n"
    "  host: <hostname>  (required)\n"
    "  auth_scheme: Bearer|token  (must pair with token_env)\n"
    "  token_env: <ENV_VAR_NAME>  (must pair with auth_scheme)\n"
    "  matches:  (optional list of match entries)\n"
    "    - paths: [{type: prefix|exact|regex, value: /...}]\n"
    "      methods: [GET, POST, ...]\n"
    "      headers: [{name: X-Hdr, value: val, type: exact|regex}]\n"
    "  git:  (optional; omit to block git clone/fetch)\n"
    "    fetch: true\n"
    "  dlp:  (optional DLP scanner overrides)\n"
    "    outbound_detectors: [token_patterns, known_secrets]\n"
    "    inbound_detectors: [naive_injection_detection]\n"
    "    outbound_on_match: block|redact|supervise  (default supervise)\n"
    "Omit any key that should use its default. "
    "`list-egress-routes` returns routes in this same format."
)


def _proposal_input_schema() -> dict[str, object]:
    """Build a fresh input schema for a routes.yaml proposal tool. Returns a
    new dict per call so the two tool definitions don't alias one object."""
    return {
        "type": "object",
        "properties": {
            "routes_yaml": {
                "type": "string",
                "description": _ROUTES_YAML_DESCRIPTION,
            },
            "justification": {
                "type": "string",
                "description": "Why this egress route is needed.",
            },
        },
        "required": ["routes_yaml", "justification"],
    }


TOOL_DEFINITIONS: list[dict[str, object]] = [
    {
        "name": _sv.TOOL_LIST_EGRESS_ROUTES,
        "description": (
            "List the current egress route table — the bottle's "
            "allowlist. Returns JSON with one entry per allowed host, "
            "each carrying its matches rules (if any) and whether "
            "the proxy injects Authorization for the route. Use this "
            "before composing an `egress-allow` or `egress-block` proposal so "
            "the new routes file extends the live one rather than "
            "replacing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_EGRESS_ALLOW,
        "description": (
            "Request operator approval to change the bottle's egress "
            "allowlist. Pass the full proposed routes.yaml content, not "
            "just the new host, plus a justification. Use "
            "`list-egress-routes` first so the proposal preserves existing "
            "routes."
        ),
        "inputSchema": _proposal_input_schema(),
    },
    {
        "name": _sv.TOOL_EGRESS_BLOCK,
        "description": (
            "Request operator approval to change the bottle's egress "
            "allowlist after a blocked outbound request. Pass the full "
            "proposed routes.yaml content plus a justification. Use "
            "`list-egress-routes` first so the proposal preserves existing "
            "routes."
        ),
        "inputSchema": _proposal_input_schema(),
    },
]


# Map each proposal tool to the input field that carries the agent's
# payload (stored in Proposal.proposed_file).
PROPOSED_FILE_FIELD: dict[str, str] = {
    _sv.TOOL_EGRESS_ALLOW: "routes_yaml",
    _sv.TOOL_EGRESS_BLOCK: "routes_yaml",
}


# --- Validation ------------------------------------------------------------


def validate_proposed_file(tool: str, content: str) -> None:
    """Syntactic validation. The operator is the real gate; this just
    catches obvious paste-errors / wrong-tool selections before they
    enter the queue."""
    if not content.strip():
        raise _RpcClientError(ERR_INVALID_PARAMS, f"{tool}: proposed file is empty")
    if tool in (_sv.TOOL_EGRESS_ALLOW, _sv.TOOL_EGRESS_BLOCK):
        try:
            config = load_config(content)
        except ValueError as e:
            raise _RpcClientError(
                ERR_INVALID_PARAMS,
                f"{tool}: proposed routes.yaml is not valid: {e}",
            ) from e
        if config.log != LOG_OFF:
            raise _RpcClientError(
                ERR_INVALID_PARAMS,
                f"{tool}: proposed routes.yaml must not change egress logging",
            )
    else:
        raise _RpcClientError(ERR_INVALID_PARAMS, f"unknown tool {tool!r}")


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
        raise _RpcClientError(ERR_INVALID_PARAMS, "tools/call missing 'name'")
    if name == _sv.TOOL_LIST_EGRESS_ROUTES:
        return handle_list_egress_routes(typing.cast(dict[str, object], params.get("arguments", {})), config)

    args_raw = params.get("arguments", {})
    if not isinstance(args_raw, dict):
        raise _RpcClientError(ERR_INVALID_PARAMS, "tools/call 'arguments' must be an object")

    justification = args_raw.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        raise _RpcClientError(
            ERR_INVALID_PARAMS,
            f"{name}: 'justification' is required and must be a non-empty string",
        )

    if name in PROPOSED_FILE_FIELD:
        file_field = PROPOSED_FILE_FIELD[name]
        proposed_file = args_raw.get(file_field)
        if not isinstance(proposed_file, str):
            raise _RpcClientError(
                ERR_INVALID_PARAMS,
                f"{name}: '{file_field}' is required and must be a string",
            )
        validate_proposed_file(name, proposed_file)
    else:
        raise _RpcClientError(ERR_INVALID_PARAMS, f"unknown tool {name!r}")

    proposal = _sv.Proposal.new(
        bottle_slug=config.bottle_slug,
        tool=name,
        proposed_file=proposed_file,
        justification=justification,
        current_file_hash=_sv.sha256_hex(proposed_file),
    )
    try:
        _sv.write_proposal(config.queue_dir, proposal)
    except OSError as e:
        raise _RpcInternalError(f"failed to write proposal to queue: {e}") from e
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
    try:
        _sv.archive_proposal(config.queue_dir, proposal.id)
    except OSError as e:
        raise _RpcInternalError(f"failed to archive proposal: {e}") from e

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


# Max request body the server accepts. 1 MB is well above any realistic
# routes.yaml proposal.
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
        except _RpcClientError as e:
            self._write_jsonrpc(jsonrpc_error(None, e.code, e.message))
            return

        config = typing.cast("MCPServer", self.server).config

        try:
            result = self._dispatch(req, config)
        except _RpcClientError as e:
            self._write_jsonrpc(jsonrpc_error(req.id, e.code, e.message))
            return
        except _RpcInternalError as e:
            cause = e.__cause__
            detail = f": {cause}" if cause else ""
            sys.stderr.write(f"supervise: internal error: {e.message}{detail}\n")
            sys.stderr.flush()
            self._write_jsonrpc(jsonrpc_error(req.id, ERR_INTERNAL, "internal error"))
            return
        except Exception as e:  # noqa: W0718 — unexpected errors
            sys.stderr.write(f"supervise: unexpected error: {type(e).__name__}: {e}\n")
            sys.stderr.flush()
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
        raise _RpcClientError(ERR_METHOD_NOT_FOUND, f"method not found: {method}")

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
