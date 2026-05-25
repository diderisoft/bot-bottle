"""Supervise sidecar HTTP server (PRD 0013).

Per-bottle MCP server exposing three tools — `egress-proxy-block`,
`pipelock-block`, `capability-block` — that the agent calls to
propose config changes when stuck. Each tool call:

  1. Validates the proposed file syntactically.
  2. Writes a Proposal to /run/supervise/queue/ (bind-mounted from
     the host's ~/.claude-bottle/queue/<slug>/).
  3. Blocks polling for a matching Response file.
  4. Returns the operator's `{status, notes}` to the agent.

The bottle slug arrives via SUPERVISE_BOTTLE_SLUG env (stamped at
container creation by the backend's start step). The queue dir comes
from SUPERVISE_QUEUE_DIR (default `/run/supervise/queue`).

Speaks MCP over HTTP+JSON-RPC. Methods handled:

  * `initialize`           — handshake; returns server info + caps.
  * `notifications/initialized` — ack-only.
  * `tools/list`           — returns the three tool definitions.
  * `tools/call`           — validates, queues, blocks, returns.

Everything else returns JSON-RPC error -32601 (method not found).

Stdlib-only. The Dockerfile copies this file + claude_bottle/supervise.py
into the image; the server imports `supervise` for the queue / Proposal
plumbing.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import typing
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Same-directory import inside the container; `supervise.py` is COPYed
# alongside this file by Dockerfile.supervise.
import supervise as _sv


# --- JSON-RPC / MCP plumbing ----------------------------------------------


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-bottle-supervise"
SERVER_VERSION = "0.1.0"

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 standard error codes.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


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
        "name": _sv.TOOL_EGRESS_PROXY_BLOCK,
        "description": (
            "Call when egress-proxy refused your HTTPS request — host "
            "without a matching route, or a path outside the route's "
            "path_allowlist (typically a 403 from the proxy). First "
            "call `list-egress-proxy-routes` to see the current route "
            "table; compose a modified version that adds or relaxes "
            "the route you need, and pass the full new file plus a "
            "justification. The operator approves or rejects in the "
            "supervise TUI. On approval the supervisor writes the "
            "new routes.yaml on the host, SIGHUPs egress-proxy (the "
            "addon's reload swaps the route table atomically without "
            "dropping in-flight connections), and mirrors the route "
            "hosts onto pipelock's allowlist so the downstream gate "
            "lets them through too."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "routes": {
                    "type": "string",
                    "description": "Full proposed routes.yaml file content (JSON text — every JSON document is valid YAML).",
                },
                "justification": {
                    "type": "string",
                    "description": "Why this routes change is justified.",
                },
            },
            "required": ["routes", "justification"],
        },
    },
    {
        "name": _sv.TOOL_LIST_EGRESS_PROXY_ROUTES,
        "description": (
            "List the current egress-proxy route table — the bottle's "
            "primary egress allowlist. Returns JSON with one entry "
            "per allowed host, each carrying its path_allowlist (if "
            "any) and whether the proxy injects Authorization for "
            "the route. Use this before composing an "
            "`egress-proxy-block` proposal so the new routes file "
            "extends the live one rather than replacing it. "
            "Pipelock's allowlist is a mirror of this set — every "
            "host listed here is also reachable through pipelock's "
            "downstream hostname gate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_PIPELOCK_BLOCK,
        "description": (
            "Call when pipelock refused your outbound request and "
            "the failing host is genuinely missing from the bottle's "
            "allowlist (vs. blocked for DLP reasons — those need a "
            "different remediation). In practice pipelock's allowlist "
            "is now a mirror of the egress-proxy routes set by "
            "`egress-proxy-block`, so prefer that tool when you want "
            "to add a host. This tool stays available for the rare "
            "case where pipelock and egress-proxy have diverged. "
            "Pass the full URL you tried to hit (scheme + host + "
            "path); the supervisor extracts the hostname and merges "
            "it into pipelock's allowlist. On approval the "
            "supervisor restarts pipelock."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "failed_url": {
                    "type": "string",
                    "description": (
                        "The full URL pipelock blocked, e.g. "
                        "https://api.github.com/repos/foo/bar. Scheme "
                        "and hostname are required; path is recorded "
                        "as operator context."
                    ),
                },
                "justification": {
                    "type": "string",
                    "description": "Why the new host should be allowed.",
                },
            },
            "required": ["failed_url", "justification"],
        },
    },
    {
        "name": _sv.TOOL_CAPABILITY_BLOCK,
        "description": (
            "Call when the bottle is missing a tool, skill, permission, "
            "or env var you need — something that lives in the agent "
            "Dockerfile rather than in routes or the pipelock allowlist. "
            "Read the current Dockerfile from "
            "/etc/claude-bottle/current-config/Dockerfile, compose a "
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


# Map each tool to the input field that carries the agent's
# tool-specific payload (stored in Proposal.proposed_file as
# free-form text the apply path interprets per tool).
#
#   egress-proxy-block: full proposed routes.yaml
#   pipelock-block:     the full failed URL (scheme + host + path) —
#                       supervisor extracts the host, merges into the
#                       bottle's current allowlist; the path is shown
#                       to the operator for context (pipelock doesn't
#                       do path-level matching).
#   capability-block:   full proposed Dockerfile
PROPOSED_FILE_FIELD: dict[str, str] = {
    _sv.TOOL_EGRESS_PROXY_BLOCK: "routes",
    _sv.TOOL_PIPELOCK_BLOCK: "failed_url",
    _sv.TOOL_CAPABILITY_BLOCK: "dockerfile",
}


# --- Validation ------------------------------------------------------------


def validate_proposed_file(tool: str, content: str) -> None:
    """Syntactic validation. The operator is the real gate; this just
    catches obvious paste-errors / wrong-tool selections before they
    enter the queue."""
    if not content.strip():
        raise _RpcError(ERR_INVALID_PARAMS, f"{tool}: proposed file is empty")
    if tool == _sv.TOOL_EGRESS_PROXY_BLOCK:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: proposed routes.yaml is not valid JSON: {e}",
            ) from e
        if not isinstance(parsed, dict) or not isinstance(parsed.get("routes"), list):
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: proposed routes.yaml must be an object with a 'routes' array",
            )
    elif tool == _sv.TOOL_PIPELOCK_BLOCK:
        # `content` is the full failed URL. Require scheme + host so
        # the supervisor can extract a hostname for the allowlist
        # merge; the path is preserved for operator context.
        parsed = urllib.parse.urlsplit(content.strip())
        if parsed.scheme not in ("http", "https"):
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: failed_url must start with http:// or https:// "
                f"(got {content!r})",
            )
        if not parsed.hostname:
            raise _RpcError(
                ERR_INVALID_PARAMS,
                f"{tool}: failed_url is missing a hostname (got {content!r})",
            )
    elif tool == _sv.TOOL_CAPABILITY_BLOCK:
        # Dockerfiles are too varied to validate syntactically beyond
        # non-empty. The operator reads the diff in the TUI.
        pass
    else:
        raise _RpcError(ERR_INVALID_PARAMS, f"unknown tool {tool!r}")


# --- MCP handlers ----------------------------------------------------------


@dataclass(frozen=True)
class ServerConfig:
    bottle_slug: str
    queue_dir: Path


def handle_initialize(_params: dict[str, object]) -> dict[str, object]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(_params: dict[str, object]) -> dict[str, object]:
    return {"tools": TOOL_DEFINITIONS}


def handle_list_egress_proxy_routes(
    _params: dict[str, object],
    _config: ServerConfig,
) -> dict[str, object]:
    """Fetch the live egress-proxy route table via its
    `_egress-proxy.local/allowlist` introspection endpoint. The
    request goes through egress-proxy as a forward proxy; the
    addon recognises the magic host and synthesizes a response —
    no real upstream connection, no allowlist enforcement
    against the magic host. Returns the JSON payload as the
    tool's text content."""
    proxy_handler = urllib.request.ProxyHandler({
        "http": _sv.EGRESS_PROXY_FORWARD_PROXY,
    })
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(_sv.EGRESS_PROXY_INTROSPECT_URL, timeout=5) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        return {
            "content": [{
                "type": "text",
                "text": (
                    f"list-egress-proxy-routes: could not reach "
                    f"{_sv.EGRESS_PROXY_INTROSPECT_URL!r} via "
                    f"{_sv.EGRESS_PROXY_FORWARD_PROXY!r}: {e}"
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
    if name == _sv.TOOL_LIST_EGRESS_PROXY_ROUTES:
        return handle_list_egress_proxy_routes(params.get("arguments", {}), config)
    if name not in PROPOSED_FILE_FIELD:
        raise _RpcError(ERR_INVALID_PARAMS, f"unknown tool {name!r}")
    args_raw = params.get("arguments", {})
    if not isinstance(args_raw, dict):
        raise _RpcError(ERR_INVALID_PARAMS, "tools/call 'arguments' must be an object")

    file_field = PROPOSED_FILE_FIELD[name]
    proposed_file = args_raw.get(file_field)
    justification = args_raw.get("justification")
    if not isinstance(proposed_file, str):
        raise _RpcError(
            ERR_INVALID_PARAMS,
            f"{name}: '{file_field}' is required and must be a string",
        )
    if not isinstance(justification, str) or not justification.strip():
        raise _RpcError(
            ERR_INVALID_PARAMS,
            f"{name}: 'justification' is required and must be a non-empty string",
        )

    validate_proposed_file(name, proposed_file)

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
    response = _sv.wait_for_response(config.queue_dir, proposal.id)
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

    def log_message(self, format: str, *args: typing.Any) -> None:
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
        except Exception as e:  # pragma: no cover — defensive
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
) -> typing.NoReturn:
    queue_dir.mkdir(parents=True, exist_ok=True)
    server = MCPServer((bind, port), MCPHandler)
    server.config = ServerConfig(bottle_slug=bottle_slug, queue_dir=queue_dir)
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
    serve(bottle_slug=bottle_slug, queue_dir=queue_dir, port=port, bind=bind)
    return 0  # serve() does not return


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
