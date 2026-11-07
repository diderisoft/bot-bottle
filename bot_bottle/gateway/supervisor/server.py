"""The supervise MCP server (HTTP + JSON-RPC), shared by every bottle.

Tools: `egress-allow`, `egress-block`, `list-egress-routes`, `forge-connect`,
`add-repo`, `create-repo`, `check-proposal`, `get-environment`.
A proposal is validated, queued with the orchestrator (`POST
/supervise/propose`, attributed by (source_ip, identity_token)), then polled
for up to `SUPERVISE_RESPONSE_TIMEOUT_SECONDS` (30). A decision in time is
returned; otherwise `status: pending` with the proposal id, which
`check-proposal` reads later. The server never opens `bot-bottle.db`.
Unattributed callers fail closed.

Methods: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`;
anything else is -32601.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import sys
import time
import typing
from dataclasses import dataclass
from urllib.parse import urlparse

from bot_bottle.contract.agent_notes import GIT_BOUNDARY
from bot_bottle.contract.forges import known_unsupported, unsupported_forge
from bot_bottle.contract.identity import IDENTITY_HEADER
from bot_bottle.gateway.bounded_http import (
    BodyReadError,
    BoundedThreadingHTTPServer,
    read_declared_body,
)
from bot_bottle.gateway.egress.schema import load_config
from bot_bottle.gateway.egress.types import LOG_OFF
from bot_bottle.gateway.policy_resolver import PolicyResolveError, PolicyResolver
from bot_bottle.gateway.supervisor.mcp_dispatch import (
    Handlers as DispatchHandlers,
    MethodNotFoundError,
    RouteResolutionError,
    dispatch,
    resolved_routes_payload,
)
from bot_bottle.contract import supervise as _sv
from bot_bottle.gateway.supervisor.env_handler import handle_get_environment
from bot_bottle.gateway.supervisor import launch_bottle_server as _lb
from bot_bottle.gateway.supervisor.forge_tools import FORGE_TOOL_DEFINITIONS


# --- JSON-RPC / MCP plumbing ---


MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "bot-bottle-supervise"
SERVER_VERSION = "0.1.0"

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 error codes.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30.0
# Poll cadence in the grace window; each poll is an HTTP round trip.
MIN_RESPONSE_POLL_INTERVAL_SECONDS = 0.25

# The orchestrator proposals are attributed through. Mandatory.
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"


@dataclass(frozen=True)
class JsonRpcRequest:
    method: str
    params: dict[str, object]
    id: object  # None for notifications
    is_notification: bool


def parse_jsonrpc(body: bytes) -> JsonRpcRequest:
    """Parse one JSON-RPC 2.0 request; ValueError with a JSON-RPC code on a bad
    shape."""
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
    """Base for errors returned as JSON-RPC error responses."""
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _RpcClientError(_RpcError):
    """A bad request; returned as-is, not logged."""


class _RpcInternalError(_RpcError):
    """A server fault; logged with its cause, returned as ERR_INTERNAL."""
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


# --- Tool definitions ---


# Shared by egress-allow and egress-block, which differ only in description.
_ROUTES_YAML_DESCRIPTION = (
    "Full proposed /etc/egress/routes.yaml content. "
    "Each route entry accepts these keys:\n"
    "  host: <hostname>  (required)\n"
    "  inspect: false  (opaque whole-host TLS tunnel; no HTTP controls)\n"
    "  inspect:  (omit for inspected defaults)\n"
    "    auth_scheme: Bearer|token  (must pair with token_env)\n"
    "    token_env: <ENV_VAR_NAME>  (must pair with auth_scheme)\n"
    "    matches:  (optional list of match entries)\n"
    "      - paths: [{type: prefix|exact|regex, value: /...}]\n"
    "        methods: [GET, POST, ...]\n"
    "        headers: [{name: X-Hdr, value: val, type: exact|regex}]\n"
    "    git:  (optional; read-only HTTPS clone/fetch for this host; push is never\n"
    "           allowed through egress, see git-gate in the tool descriptions)\n"
    "      fetch: true\n"
    "    outbound_detectors: [token_patterns, known_secrets]\n"
    "    inbound_detectors: [naive_injection_detection]\n"
    "    outbound_on_match: block|redact|supervise  (default supervise)\n"
    "Omit any key that should use its default. "
    "`list-egress-routes` returns routes in this same format."
)


def _proposal_input_schema() -> dict[str, object]:
    """A fresh input schema for a routes.yaml proposal tool (not shared between
    tools)."""
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
            "replacing it. " + GIT_BOUNDARY
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
            "Request operator approval to change the egress allowlist. "
            "Pass the full routes.yaml (not just the host) plus a justification; "
            "use `list-egress-routes` first. Allowing a host doesn't let "
            "git through to it: "
            + GIT_BOUNDARY
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
    *FORGE_TOOL_DEFINITIONS,
    _lb.TOOL_DEFINITION,
    {
        "name": _sv.TOOL_CHECK_PROPOSAL,
        "description": (
            "Poll a pending proposal WITHOUT re-queuing. Pass the `proposal_id` "
            "from a `pending` response. Returns `pending`, `approved`, `modified`, "
            "`rejected`, or `unknown` (wrong id or already resolved)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string",
                                "description": "The proposal id from a `pending` response."},
            },
            "required": ["proposal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": _sv.TOOL_GET_ENVIRONMENT,
        "description": "Return read-only information about the running bot-bottle "
                       "environment: orchestrator version, git commit, and protocol.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# Each proposal tool's payload field (stored as `Proposal.proposed_file`).
PROPOSED_FILE_FIELD: dict[str, str] = {
    _sv.TOOL_EGRESS_ALLOW: "routes_yaml",
    _sv.TOOL_EGRESS_BLOCK: "routes_yaml",
    _sv.TOOL_FORGE_CONNECT: "url",
    _sv.TOOL_ADD_REPO: "repo",
    _sv.TOOL_CREATE_REPO: "repo",
}
# `<forge>/<owner>/<name>`, a repo to create (no alias, no access suffix).
_NEW_REPO_RE = re.compile(r"^([A-Za-z0-9._-]+(?::[0-9]+)?)/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
PUBLIC_SUFFIX = ":public"
# `[<alias>=]<forge>/<owner>/<repo>[:write]`, as `manifest.forges` reads it.
_GRANT_RE = re.compile(r"^(?:([A-Za-z0-9._-]+)=)?([A-Za-z0-9._@:-]+)/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(:write)?$")


# --- Validation ---


def validate_proposed_file(tool: str, content: str) -> None:
    """Syntactic checks to catch obvious mistakes; the operator is the real
    gate."""
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
    elif tool == _sv.TOOL_FORGE_CONNECT:
        parsed = urlparse(content.strip())
        if (parsed.scheme != "https" or not parsed.hostname
                or any(c.isspace() for c in content.strip())):
            raise _RpcClientError(ERR_INVALID_PARAMS,
                                  f"{tool}: 'url' must be the forge's https URL")
        _refuse_unsupported_forge(tool, parsed.hostname)
    elif tool == _sv.TOOL_CREATE_REPO:
        if not (m := _NEW_REPO_RE.match(content.strip().removesuffix(PUBLIC_SUFFIX))):
            raise _RpcClientError(ERR_INVALID_PARAMS,
                                  f"{tool}: 'repo' is <forge>/<owner>/<name>")
        _refuse_unsupported_forge(tool, m.group(1).split(":", 1)[0])
    elif tool == _sv.TOOL_ADD_REPO:
        if not (m := _GRANT_RE.match(content.strip())):
            raise _RpcClientError(ERR_INVALID_PARAMS,
                                  f"{tool}: 'repo' is [alias=]<forge>/<owner>/<repo>[:write]")
        _refuse_unsupported_forge(tool, m.group(2).rsplit("@", 1)[-1].split(":", 1)[0])
    else:
        raise _RpcClientError(ERR_INVALID_PARAMS, f"unknown tool {tool!r}")


def _refuse_unsupported_forge(tool: str, host: str) -> None:
    """A public forge of a kind bot-bottle can't connect, refused before it's
    queued. Any other host is asked by the host controller, which refuses it
    the same way when it isn't Gitea or GitHub."""
    if known_unsupported(host):
        raise _RpcClientError(ERR_INVALID_PARAMS, f"{tool}: {unsupported_forge(host)}")


def _build_launch_bottle_proposed(tool: str, args_raw: dict[str, object]) -> str:
    try:
        return _lb.build_proposed(tool, args_raw)
    except _lb._ArgError as e:
        raise _RpcClientError(ERR_INVALID_PARAMS, str(e)) from e


# --- MCP handlers ---


@dataclass(frozen=True)
class ServerConfig:
    # No bottle slug: each request is attributed by the control plane.
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS


# What the server tells the agent up front (MCP `instructions`).
SERVER_INSTRUCTIONS = (
    "Ask the operator for access this bottle doesn't have. Network: "
    "`list-egress-routes`, then `egress-allow` with the full routes file. "
    "Each request queues a proposal; if it comes back pending, poll it with "
    "`check-proposal` rather than asking again. A git forge (Gitea or GitHub) "
    "this host hasn't connected: `forge-connect`; a repo on one: `add-repo`; "
    "a new repo on one: `create-repo`. "
    "To run another agent in a child bottle: `launch-bottle`. "
    + GIT_BOUNDARY
)


def handle_initialize(_params: dict[str, object]) -> dict[str, object]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": SERVER_INSTRUCTIONS,
    }


def handle_tools_list(_params: dict[str, object]) -> dict[str, object]:
    return {"tools": TOOL_DEFINITIONS}


def handle_tools_call(
    params: dict[str, object],
    config: ServerConfig,
    *,
    resolver: "PolicyResolver",
    source_ip: str,
    identity_token: str,
) -> dict[str, object]:
    """Validate, queue over the control plane, poll through the grace window,
    and return the result as MCP `content`. `list-egress-routes` is answered
    earlier, in `MCPHandler._dispatch`."""
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

    if name == _sv.TOOL_LAUNCH_BOTTLE:
        proposed_file = _build_launch_bottle_proposed(name, args_raw)
    elif name in PROPOSED_FILE_FIELD:
        file_field = PROPOSED_FILE_FIELD[name]
        proposed_file = args_raw.get(file_field)
        if not isinstance(proposed_file, str):
            raise _RpcClientError(
                ERR_INVALID_PARAMS,
                f"{name}: '{file_field}' is required and must be a string",
            )
        if name == _sv.TOOL_CREATE_REPO:
            public = args_raw.get("public", False)
            if not isinstance(public, bool):
                raise _RpcClientError(ERR_INVALID_PARAMS, f"{name}: 'public' is true or false")
            proposed_file = proposed_file.strip() + (PUBLIC_SUFFIX if public else "")
        validate_proposed_file(name, proposed_file)
    else:
        raise _RpcClientError(ERR_INVALID_PARAMS, f"unknown tool {name!r}")

    proposal_id = _queue_proposal(
        resolver, source_ip, identity_token,
        tool=name, proposed_file=proposed_file, justification=justification,
    )
    sys.stderr.write(
        f"supervise: queued proposal {proposal_id} ({name}); waiting for operator...\n"
    )
    sys.stderr.flush()

    deadline = time.monotonic() + config.response_timeout_seconds
    while time.monotonic() < deadline:
        response = _poll_response(resolver, source_ip, identity_token, proposal_id)
        if response is not None:
            return {
                "content": [{"type": "text", "text": format_response_text(response)}],
                "isError": response.status == _sv.STATUS_REJECTED,
            }
        time.sleep(MIN_RESPONSE_POLL_INTERVAL_SECONDS)

    text = format_pending_response_text(proposal_id, config.response_timeout_seconds)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


def _queue_proposal(
    resolver: "PolicyResolver",
    source_ip: str,
    identity_token: str,
    *,
    tool: str,
    proposed_file: str,
    justification: str,
) -> str:
    """Queue a proposal and return its id; fail-closed outcomes become internal
    errors (-32603) without leaking the cause."""
    try:
        proposal_id = resolver.propose_supervise(
            source_ip, identity_token,
            tool=tool, proposed_file=proposed_file, justification=justification,
        )
    except PolicyResolveError as e:
        raise _RpcInternalError(f"orchestrator unreachable, cannot queue proposal: {e}") from e
    if proposal_id is None:
        raise _RpcInternalError("request source is not attributed to a bottle")
    return proposal_id


def _poll_response(
    resolver: "PolicyResolver", source_ip: str, identity_token: str, proposal_id: str,
) -> "_sv.Response | None":
    """One poll for `proposal_id`. The Response once decided, else None
    (`unknown` counts as still waiting)."""
    try:
        result = resolver.poll_supervise(source_ip, identity_token, proposal_id)
    except PolicyResolveError as e:
        raise _RpcInternalError(f"orchestrator unreachable, cannot poll proposal: {e}") from e
    if result is None:
        raise _RpcInternalError("request source is not attributed to a bottle")
    if result.get("status") in _sv.STATUSES:
        return _response_from_poll(proposal_id, result)
    return None


def _response_from_poll(proposal_id: str, result: "dict[str, object]") -> "_sv.Response":
    """A Response from a decided `/supervise/poll` payload."""
    final_file = result.get("final_file")
    return _sv.Response(
        proposal_id=proposal_id,
        status=str(result.get("status")),
        notes=str(result.get("notes") or ""),
        final_file=final_file if isinstance(final_file, str) else None,
    )


def handle_check_proposal(
    params: dict[str, object],
    *,
    resolver: "PolicyResolver",
    source_ip: str,
    identity_token: str,
) -> dict[str, object]:
    """Non-blocking poll of a proposal by id, attributed so a bottle reads only
    its own. Never creates a Proposal. The orchestrator archives a decided
    proposal on read."""
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
        result = resolver.poll_supervise(source_ip, identity_token, proposal_id)
    except PolicyResolveError as e:
        raise _RpcInternalError(f"orchestrator unreachable, cannot poll proposal: {e}") from e
    if result is None:
        raise _RpcInternalError("request source is not attributed to a bottle")
    status = result.get("status")

    if status == _sv.POLL_STATUS_UNKNOWN:
        return {
            "content": [{"type": "text", "text": format_unknown_proposal_text(proposal_id)}],
            "isError": True,
        }
    if status == _sv.POLL_STATUS_PENDING:
        return {
            "content": [{"type": "text", "text": format_still_pending_text(proposal_id)}],
            "isError": False,
        }
    response = _response_from_poll(proposal_id, result)
    return {
        "content": [{"type": "text", "text": format_response_text(response)}],
        "isError": response.status == _sv.STATUS_REJECTED,
    }


def format_response_text(response: "_sv.Response") -> str:
    """A Response as the tool's text content."""
    lines = [f"status: {response.status}"]
    if response.notes:
        lines.append(f"notes: {response.notes}")
    if response.status == _sv.STATUS_MODIFIED and response.final_file is not None:
        lines.append("the operator modified your proposal before approving; "
                     "the final config is now what's been applied")
    return "\n".join(lines)


def format_pending_response_text(proposal_id: str, timeout_seconds: float) -> str:
    """Grace-window timeout: the proposal stays queued and the agent gets its
    id for `check-proposal`."""
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


# --- HTTP transport ---


# Max request body.
MAX_BODY_BYTES = 1 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0
MAX_REQUEST_WORKERS = 32


class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Per-request JSON-RPC handler; the threading server lets calls block
    concurrently."""

    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def log_message(self, format: str, *args: typing.Any) -> None:  # noqa: A002
        if os.environ.get("SUPERVISE_DEBUG"):
            super().log_message(format, *args)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_text(200, "ok\n")
            return
        if self.path == "/version":
            body = json.dumps({
                "version": os.environ.get("BOT_BOTTLE_VERSION", "unknown"),
                "commit": os.environ.get("BOT_BOTTLE_COMMIT", ""),
                "protocol": int(os.environ.get("BOT_BOTTLE_PROTOCOL", "0") or "0"),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self._write_text(405, "use POST for MCP requests\n")

    def do_POST(self) -> None:
        try:
            body = read_declared_body(
                self.rfile,
                self.connection,
                self.headers.get("Content-Length"),
                maximum=MAX_BODY_BYTES,
                timeout_seconds=REQUEST_BODY_TIMEOUT_SECONDS,
                require_length=True,
            )
        except BodyReadError as exc:
            self._write_text(exc.status, exc.message + "\n")
            return

        try:
            req = parse_jsonrpc(body)
        except _RpcClientError as e:
            self._write_jsonrpc(jsonrpc_error(None, e.code, e.message))
            return

        config = typing.cast("MCPServer", self.server).config

        try:
            result = self._dispatch(req, config)
        except MethodNotFoundError as e:
            self._write_jsonrpc(
                jsonrpc_error(req.id, ERR_METHOD_NOT_FOUND, f"method not found: {e}"),
            )
            return
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
        def check(params: dict[str, object]) -> object:
            return handle_check_proposal(
                params,
                resolver=self._resolver_or_fail(),
                source_ip=self.client_address[0],
                identity_token=self._identity_token(),
            )

        def propose(params: dict[str, object]) -> object:
            return handle_tools_call(
                params,
                config,
                resolver=self._resolver_or_fail(),
                source_ip=self.client_address[0],
                identity_token=self._identity_token(),
            )

        def list_routes(_params: dict[str, object]) -> object:
            try:
                return resolved_routes_payload(
                    self._resolver_or_fail(),
                    self.client_address[0],
                    self._identity_token(),
                )
            except RouteResolutionError as exc:
                raise _RpcInternalError(
                    f"could not resolve live egress routes: {exc}"
                ) from exc

        return dispatch(req, DispatchHandlers(
            initialize=handle_initialize,
            tools_list=handle_tools_list,
            list_routes=list_routes,
            check_proposal=check,
            get_environment=handle_get_environment,
            propose=propose,
        ))

    def _identity_token(self) -> str:
        """The identity token from the request header, or "". A wrong or
        missing one fails at the control plane."""
        headers = getattr(self, "headers", None)
        return headers.get(IDENTITY_HEADER, "") if headers is not None else ""

    def _resolver_or_fail(self) -> "PolicyResolver":
        """This server's resolver; fail closed without one."""
        resolver = getattr(self.server, "policy_resolver", None)
        if resolver is None:
            raise _RpcInternalError("supervise server has no policy resolver")
        return resolver

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


class MCPServer(BoundedThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    config: ServerConfig = ServerConfig()
    # Set by `serve`.
    policy_resolver: "PolicyResolver | None" = None

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, max_workers=MAX_REQUEST_WORKERS, **kwargs)


# --- Entry point ---


def serve(
    *,
    resolver: "PolicyResolver",
    port: int = _sv.SUPERVISE_PORT,
    bind: str = "0.0.0.0",
    response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
) -> typing.NoReturn:
    server = MCPServer((bind, port), MCPHandler)
    server.config = ServerConfig(response_timeout_seconds=response_timeout_seconds)
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
    del argv  # env-only
    orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    if not orch_url:
        # Without an orchestrator nothing can be attributed: don't serve.
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
    return 0  # serve() doesn't return


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
