"""Supervise daemon HTTP server (PRD 0013).

Per-bottle MCP server exposing tools the agent calls to propose egress
config changes when stuck. The tools are `egress-allow`,
`egress-block`, `list-egress-routes`, and `check-proposal`.

Each queued proposal tool call:

  1. Validates the proposed file syntactically.
  2. Writes a Proposal to the host SQLite database.
  3. Blocks polling for a matching Response row, up to a short grace
     window (`SUPERVISE_RESPONSE_TIMEOUT_SECONDS`, default 30s).
  4. On a decision within the window, returns the operator's
     `{status, notes}`. On timeout, returns `status: pending` **with the
     proposal id** and leaves the proposal queued — the flow is
     non-blocking past the grace window (PRD prd-new / issue #412).

`check-proposal` is the non-blocking companion: given a `proposal_id`
returned by a `pending` response, it reports the current decision
(`pending` | `approved` | `modified` | `rejected`) without re-proposing,
so an approval made out-of-band (e.g. a web review console) can be resumed
without holding an HTTP request open.

One shared server fronts every bottle (PRD 0070) and attributes each
proposal to the calling bottle by source IP, resolved from the orchestrator
— an unattributed or unreachable source fails closed. BOT_BOTTLE_ORCHESTRATOR_URL
is mandatory: there is no fixed-slug single-tenant fallback. SUPERVISE_DB_PATH
points at the bind-mounted host database.

Speaks MCP over HTTP+JSON-RPC. Methods handled:

  * `initialize`           — handshake; returns server info + caps.
  * `notifications/initialized` — ack-only.
  * `tools/list`           — returns the tool definitions.
  * `tools/call`           — validates, queues, waits out the grace
                             window, returns (pending past it); or, for
                             `check-proposal`, a non-blocking status poll.

Everything else returns JSON-RPC error -32601 (method not found).

The Dockerfile copies this script to /app/supervise_server.py and installs
the bot_bottle package so its `from bot_bottle.*` imports resolve.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import time
import typing
from dataclasses import dataclass, replace

from bot_bottle.constants import IDENTITY_HEADER
from bot_bottle.egress_addon_core import (
    LOG_OFF, load_config, resolve_client_context, route_to_yaml_dict,
)
from bot_bottle.policy_resolver import PolicyResolveError, PolicyResolver
from bot_bottle import supervise as _sv


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

# The per-host orchestrator control plane the shared supervise server attributes
# each proposal to, by source IP. Mandatory — there is no single-tenant
# SUPERVISE_BOTTLE_SLUG fallback.
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"


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
    {
        "name": _sv.TOOL_CHECK_PROPOSAL,
        "description": (
            "Poll a previously queued proposal for the operator's decision "
            "WITHOUT blocking or re-proposing. Pass the `proposal_id` you "
            "got back when an `egress-allow`/`egress-block` call returned "
            "`status: pending`. Returns the current status: `pending` (no "
            "decision yet — poll again later), `approved`, `modified`, "
            "`rejected`, or `unknown` (no such queued proposal — wrong id, "
            "or it was already resolved and read)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "description": (
                        "The proposal id from a `pending` response."
                    ),
                },
            },
            "required": ["proposal_id"],
            "additionalProperties": False,
        },
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
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS


def handle_initialize(_params: dict[str, object]) -> dict[str, object]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_tools_list(_params: dict[str, object]) -> dict[str, object]:
    return {"tools": TOOL_DEFINITIONS}


def handle_tools_call(
    params: dict[str, object],
    config: ServerConfig,
) -> dict[str, object]:
    """Validates the proposal, writes it to the queue, blocks waiting
    for a Response, returns the result wrapped in MCP `content`.

    `list-egress-routes` never reaches here — the handler answers it from
    the calling bottle's resolved policy before dispatching (see
    `MCPHandler._dispatch`); this path is the queued, operator-approved
    `egress-allow` / `egress-block` tools."""
    name = params.get("name")
    if not isinstance(name, str):
        raise _RpcClientError(ERR_INVALID_PARAMS, "tools/call missing 'name'")

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
        _sv.write_proposal(proposal)
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
            config.bottle_slug,
            proposal.id,
            poll_interval=MIN_RESPONSE_POLL_INTERVAL_SECONDS,
            deadline=deadline,
        )
    except TimeoutError:
        text = format_pending_response_text(proposal.id, config.response_timeout_seconds)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
    try:
        _sv.archive_proposal(config.bottle_slug, proposal.id)
    except OSError as e:
        raise _RpcInternalError(f"failed to archive proposal: {e}") from e

    text = format_response_text(response)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": response.status == _sv.STATUS_REJECTED,
    }


def handle_check_proposal(
    params: dict[str, object],
    config: ServerConfig,
) -> dict[str, object]:
    """Non-blocking poll of a queued proposal's decision, by id.

    Never creates a Proposal (so `check-proposal` isn't in `TOOLS`); it only
    reads the queue. Resolution order mirrors the synchronous path's terminal
    step — a decided proposal is archived here exactly as `handle_tools_call`
    archives it after `wait_for_response`, so `pending` proposals stay visible
    to the operator until they're both decided *and* polled."""
    args_raw = params.get("arguments", {})
    if not isinstance(args_raw, dict):
        raise _RpcClientError(ERR_INVALID_PARAMS, "tools/call 'arguments' must be an object")
    proposal_id = args_raw.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise _RpcClientError(
            ERR_INVALID_PARAMS,
            "check-proposal: 'proposal_id' is required and must be a non-empty string",
        )
    proposal_id = proposal_id.strip()

    try:
        response = _sv.read_response(config.bottle_slug, proposal_id)
    except FileNotFoundError:
        # No decision yet — distinguish "still queued" from "unknown id".
        try:
            _sv.read_proposal(config.bottle_slug, proposal_id)
        except FileNotFoundError:
            return {
                "content": [{"type": "text", "text": format_unknown_proposal_text(proposal_id)}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": format_still_pending_text(proposal_id)}],
            "isError": False,
        }

    try:
        _sv.archive_proposal(config.bottle_slug, proposal_id)
    except OSError as e:
        raise _RpcInternalError(f"failed to archive proposal: {e}") from e
    return {
        "content": [{"type": "text", "text": format_response_text(response)}],
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


def format_pending_response_text(proposal_id: str, timeout_seconds: float) -> str:
    """Grace-window timeout: the proposal stays queued, and the agent is
    told the id so it can `check-proposal` instead of re-proposing."""
    return "\n".join([
        "status: pending",
        f"proposal_id: {proposal_id}",
        (
            f"notes: no operator decision within {timeout_seconds:g}s; the "
            "proposal remains queued. Poll it (do not re-propose) by calling "
            f"`check-proposal` with proposal_id={proposal_id!r}."
        ),
    ])


def format_still_pending_text(proposal_id: str) -> str:
    return "\n".join([
        "status: pending",
        f"proposal_id: {proposal_id}",
        "notes: still queued; no operator decision yet. Call `check-proposal` again later.",
    ])


def format_unknown_proposal_text(proposal_id: str) -> str:
    return "\n".join([
        "status: unknown",
        f"proposal_id: {proposal_id}",
        (
            "notes: no queued proposal with this id for this bottle — the id "
            "may be wrong, or the proposal was already resolved and read."
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
            # `list-egress-routes` is read-only introspection. The shared gateway
            # has no static route table (routes are resolved per request by
            # source IP), so answer it from the calling bottle's resolved policy.
            # Otherwise the agent sees an empty allowlist and composes an egress
            # proposal that *replaces* the live routes instead of extending them
            # — silently dropping base routes like api.anthropic.com on approval.
            if req.params.get("name") == _sv.TOOL_LIST_EGRESS_ROUTES:
                return self._resolved_routes_payload()
            # `check-proposal` is a non-blocking read of the calling bottle's
            # own queue — attributed by source IP like a proposal, but it
            # never queues or blocks.
            if req.params.get("name") == _sv.TOOL_CHECK_PROPOSAL:
                return handle_check_proposal(req.params, self._attributed_config(config))
            # Attribute the proposal to the source-IP-resolved bottle, so the one
            # shared server queues each bottle's proposal under its own slug.
            return handle_tools_call(req.params, self._attributed_config(config))
        raise _RpcClientError(ERR_METHOD_NOT_FOUND, f"method not found: {method}")

    def _resolver_or_fail(self) -> "PolicyResolver":
        """This server's policy resolver. A server started without one is a
        misconfiguration, not a tenancy mode — fail closed rather than
        attribute (or list) anything."""
        resolver = getattr(self.server, "policy_resolver", None)
        if resolver is None:
            raise _RpcInternalError("supervise server has no policy resolver")
        return resolver

    def _resolved_routes_payload(self) -> dict[str, object]:
        """The calling bottle's live egress routes as the `list-egress-routes`
        JSON payload, resolved by (source_ip, identity token). Fail-closed like
        `_attributed_config`: an unattributed source or an unreachable
        orchestrator yields an empty route list (never another bottle's),
        courtesy of `resolve_client_context`."""
        resolver = self._resolver_or_fail()
        headers = getattr(self, "headers", None)
        token = headers.get(IDENTITY_HEADER, "") if headers is not None else ""
        conf, _slug, _tokens = resolve_client_context(
            resolver, self.client_address[0], token,
        )
        body = json.dumps(
            {"routes": [route_to_yaml_dict(r) for r in conf.routes]}, indent=2,
        )
        return {"content": [{"type": "text", "text": body}], "isError": False}

    def _attributed_config(self, config: ServerConfig) -> ServerConfig:
        """The ServerConfig with `bottle_slug` bound to *this request's* bottle:
        the bottle id attributed from the source IP — **fail-closed**, an
        unattributed or unreachable source raises so no proposal is queued under
        the wrong (or empty) slug."""
        resolver = self._resolver_or_fail()
        # The agent's MCP client sends the identity token as a request header
        # (provisioned via `mcp add --header`); the orchestrator requires the
        # (source_ip, token) pair, so a missing/wrong token fail-closes below.
        headers = getattr(self, "headers", None)
        token = headers.get(IDENTITY_HEADER, "") if headers is not None else ""
        try:
            bottle_id = resolver.resolve_bottle_id(self.client_address[0], token)
        except PolicyResolveError as e:
            raise _RpcInternalError(f"orchestrator unreachable, cannot attribute: {e}") from e
        if not bottle_id:
            raise _RpcInternalError("request source is not attributed to a bottle")
        return replace(config, bottle_slug=bottle_id)

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
    config: ServerConfig = ServerConfig(bottle_slug="")
    # Set by `serve`; every proposal is attributed to the source-IP-resolved
    # bottle. The class default is a placeholder — a server without a resolver
    # fails closed per request (see `_resolver_or_fail`).
    policy_resolver: "PolicyResolver | None" = None


# --- Entry point -----------------------------------------------------------


def serve(
    *,
    resolver: "PolicyResolver",
    port: int = _sv.SUPERVISE_PORT,
    bind: str = "0.0.0.0",
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
) -> typing.NoReturn:
    server = MCPServer((bind, port), MCPHandler)
    # bottle_slug is a placeholder: every request's proposal is attributed to
    # the source-IP-resolved bottle (see MCPHandler._attributed_config).
    server.config = ServerConfig(
        bottle_slug="",
        response_timeout_seconds=response_timeout_seconds,
    )
    server.policy_resolver = resolver
    sys.stderr.write(
        f"supervise listening on {bind}:{port}; multi-tenant; "
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
    orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    if not orch_url:
        # Resolver-only: without an orchestrator the server can't attribute a
        # proposal to a bottle, so it must not serve (fail-closed).
        sys.stderr.write(
            f"supervise: {ORCHESTRATOR_URL_ENV} is required "
            "(no single-tenant SUPERVISE_BOTTLE_SLUG fallback)\n"
        )
        return 2
    port = int(os.environ.get("SUPERVISE_PORT", str(_sv.SUPERVISE_PORT)))
    bind = os.environ.get("SUPERVISE_BIND", "0.0.0.0")
    try:
        response_timeout_seconds = _response_timeout_from_env(os.environ)
    except ValueError as e:
        sys.stderr.write(f"supervise: {e}\n")
        return 2
    serve(
        resolver=PolicyResolver(orch_url),
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
