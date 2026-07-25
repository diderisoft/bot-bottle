"""Unit: supervise daemon MCP server (PRD 0013, PRD 0070).

The daemon no longer opens bot-bottle.db: it queues proposals and polls for
their responses over the control-plane RPC (issue #469). These tests drive the
handlers with `_FakeSuperviseResolver`, an in-process stand-in for
`PolicyResolver.propose_supervise` / `poll_supervise` backed by the real queue
store — so the operator-response and archive contracts are still exercised
end-to-end, just through the RPC seam instead of a direct file handle."""

import http.client
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from tests.unit import use_bottle_root

from bot_bottle.orchestrator import supervisor as _sv
from bot_bottle.orchestrator.store import queue_store as _qs
from bot_bottle.store import audit_store as _as

from bot_bottle.gateway.supervisor import server as supervise_server  # noqa: E402
from bot_bottle.gateway.supervisor.server import (
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    MCPHandler,
    MCPServer,
    PROPOSED_FILE_FIELD,
    ServerConfig,
    TOOL_DEFINITIONS,
    _RpcClientError,
    _RpcError,
    _RpcInternalError,
    _response_timeout_from_env,
    format_pending_response_text,
    format_response_text,
    handle_check_proposal,
    handle_initialize,
    handle_tools_call,
    handle_tools_list,
    jsonrpc_error,
    jsonrpc_result,
    parse_jsonrpc,
    validate_proposed_file,
)

# The orchestrator's supervise service, backing the fake resolver's queue reads
# and writes (the fake stands in for the RPC; state still goes through the real
# queue store).
_SV = _sv.Supervisor()

# Fixed caller identity for the handler tests. The control plane attributes by
# (source_ip, identity_token); the fake resolver ignores them and answers for a
# fixed bottle, since attribution itself is covered by the orchestrator tests.
_SRC = "10.0.0.7"
_TOK = "tok"


class _FakeSuperviseResolver:
    """Stand-in for `PolicyResolver`'s supervise RPCs, backed by the real queue
    store (as the orchestrator is). `bottle_id=None` models an unattributed
    caller (a clean 403 → None); `raises=True` models an unreachable
    orchestrator (`PolicyResolveError`)."""

    def __init__(
        self, bottle_id: str | None = "dev", raises: bool = False, policy: str = "",
        poll_raises: bool = False, poll_none: bool = False,
    ) -> None:
        self.bottle_id = bottle_id
        self.raises = raises
        self._policy = policy
        # propose succeeds, but the later poll fails — models an orchestrator
        # that becomes unreachable (poll_raises) or unattributes mid-flight
        # (poll_none) after the proposal was queued.
        self.poll_raises = poll_raises
        self.poll_none = poll_none

    def propose_supervise(
        self, source_ip: str, identity_token: str, *,
        tool: str, proposed_file: str, justification: str,
    ) -> str | None:
        del source_ip, identity_token
        if self.raises:
            raise supervise_server.PolicyResolveError("orchestrator down")
        if self.bottle_id is None:
            return None
        proposal = _sv.Proposal.new(
            bottle_slug=self.bottle_id, tool=tool, proposed_file=proposed_file,
            justification=justification,
        )
        _SV.write_proposal(proposal)
        return proposal.id

    def poll_supervise(
        self, source_ip: str, identity_token: str, proposal_id: str,
    ) -> dict[str, object] | None:
        del source_ip, identity_token
        if self.raises or self.poll_raises:
            raise supervise_server.PolicyResolveError("orchestrator down")
        if self.bottle_id is None or self.poll_none:
            return None
        try:
            response = _SV.read_response(self.bottle_id, proposal_id)
        except FileNotFoundError:
            try:
                _SV.read_proposal(self.bottle_id, proposal_id)
            except FileNotFoundError:
                return {"status": _sv.POLL_STATUS_UNKNOWN}
            return {"status": _sv.POLL_STATUS_PENDING}
        # Idempotent: poll never archives (issue #469 review).
        return {
            "status": response.status, "notes": response.notes,
            "final_file": response.final_file,
        }

    # Used by list-egress-routes (`_resolved_routes_payload`), unchanged path.
    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = "",
    ) -> tuple[str | None, str | None, dict[str, str]]:
        del source_ip, identity_token
        if self.raises:
            raise supervise_server.PolicyResolveError("orchestrator down")
        return self._policy, self.bottle_id, {}


def _tools_call(
    resolver: object, params: dict[str, object],
    config: "ServerConfig | None" = None,
) -> dict[str, object]:
    return handle_tools_call(
        params, config or ServerConfig(),
        resolver=resolver, source_ip=_SRC, identity_token=_TOK,  # type: ignore[arg-type]
    )


def _check(resolver: object, params: dict[str, object]) -> dict[str, object]:
    return handle_check_proposal(
        params, resolver=resolver, source_ip=_SRC, identity_token=_TOK,  # type: ignore[arg-type]
    )


# --- Validation ------------------------------------------------------------


class TestValidation(unittest.TestCase):
    def test_empty_proposed_file_rejected_for_tools_with_file_field(self):
        with self.assertRaises(_RpcError):
            validate_proposed_file(_sv.TOOL_EGRESS_ALLOW, "   \n\t")

    def test_capability_block_rejected_as_unknown_tool(self):
        with self.assertRaises(_RpcError) as cm:
            validate_proposed_file("capability-block", "FROM python:3.13\n")
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("unknown tool", cm.exception.message)

    def test_egress_routes_yaml_is_validated(self):
        validate_proposed_file(
            _sv.TOOL_EGRESS_ALLOW,
            "routes:\n  - host: example.com\n",
        )

    def test_invalid_egress_routes_yaml_rejected(self):
        with self.assertRaises(_RpcError):
            validate_proposed_file(_sv.TOOL_EGRESS_BLOCK, "routes: nope\n")

    def test_egress_routes_yaml_rejects_log_full(self):
        with self.assertRaises(_RpcError) as cm:
            validate_proposed_file(
                _sv.TOOL_EGRESS_ALLOW,
                "log: 2\nroutes:\n  - host: example.com\n",
            )
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("must not change egress logging", cm.exception.message)


# --- Error taxonomy --------------------------------------------------------


class TestRpcErrorTaxonomy(unittest.TestCase):
    def test_rpc_client_error_is_rpc_error(self):
        e = _RpcClientError(ERR_INVALID_PARAMS, "bad param")
        self.assertIsInstance(e, _RpcError)
        self.assertEqual(ERR_INVALID_PARAMS, e.code)
        self.assertEqual("bad param", e.message)

    def test_rpc_internal_error_is_rpc_error(self):
        e = _RpcInternalError("disk full")
        self.assertIsInstance(e, _RpcError)
        self.assertEqual(ERR_INTERNAL, e.code)
        self.assertEqual("disk full", e.message)

    def test_rpc_internal_error_preserves_cause(self):
        cause = OSError("no space left on device")
        try:
            raise _RpcInternalError("failed to write") from cause
        except _RpcInternalError as e:
            self.assertIs(cause, e.__cause__)

    def test_parse_error_is_client_error(self):
        with self.assertRaises(_RpcClientError):
            parse_jsonrpc(b"{bad json")

    def test_validation_error_is_client_error(self):
        with self.assertRaises(_RpcClientError):
            validate_proposed_file(_sv.TOOL_EGRESS_ALLOW, "routes: nope\n")

    def test_unknown_tool_in_tools_call_is_client_error(self):
        with self.assertRaises(_RpcClientError) as cm:
            _tools_call(_FakeSuperviseResolver(), {"name": "no-such-tool", "arguments": {}})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)


class TestRpcInternalErrorOnRpcFailure(unittest.TestCase):
    """A queue RPC that can't reach the orchestrator (or returns unattributed)
    surfaces as ERR_INTERNAL — the daemon fails closed rather than leaking the
    cause to the agent."""

    _ARGS: dict[str, object] = {
        "name": _sv.TOOL_EGRESS_ALLOW,
        "arguments": {
            "routes_yaml": "routes:\n  - host: example.com\n",
            "justification": "x",
        },
    }

    def test_unreachable_orchestrator_raises_internal(self):
        with self.assertRaises(_RpcInternalError) as cm:
            _tools_call(_FakeSuperviseResolver(raises=True), self._ARGS)
        self.assertEqual(ERR_INTERNAL, cm.exception.code)
        self.assertIsNotNone(cm.exception.__cause__)

    def test_unattributed_source_raises_internal(self):
        with self.assertRaises(_RpcInternalError) as cm:
            _tools_call(_FakeSuperviseResolver(bottle_id=None), self._ARGS)
        self.assertEqual(ERR_INTERNAL, cm.exception.code)


# --- JSON-RPC parsing ------------------------------------------------------


class TestParseJsonRpc(unittest.TestCase):
    def test_parses_request_with_id(self):
        req = parse_jsonrpc(
            b'{"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}'
        )
        self.assertEqual("tools/list", req.method)
        self.assertEqual(7, req.id)
        self.assertFalse(req.is_notification)

    def test_parses_notification_no_id(self):
        req = parse_jsonrpc(
            b'{"jsonrpc": "2.0", "method": "notifications/initialized"}'
        )
        self.assertTrue(req.is_notification)
        self.assertIsNone(req.id)

    def test_rejects_bad_json(self):
        with self.assertRaises(_RpcError) as cm:
            parse_jsonrpc(b"{not json")
        self.assertEqual(ERR_PARSE, cm.exception.code)

    def test_rejects_wrong_jsonrpc_version(self):
        with self.assertRaises(_RpcError) as cm:
            parse_jsonrpc(b'{"jsonrpc": "1.0", "method": "x"}')
        self.assertEqual(ERR_INVALID_REQUEST, cm.exception.code)

    def test_rejects_missing_method(self):
        with self.assertRaises(_RpcError):
            parse_jsonrpc(b'{"jsonrpc": "2.0"}')

    def test_treats_null_id_as_request(self):
        # JSON-RPC spec: id can be null for a request (just discouraged).
        req = parse_jsonrpc(b'{"jsonrpc": "2.0", "id": null, "method": "x"}')
        self.assertFalse(req.is_notification)
        self.assertIsNone(req.id)


# --- JSON-RPC response framing --------------------------------------------


class TestJsonRpcFraming(unittest.TestCase):
    def test_result_envelope(self):
        body = jsonrpc_result(1, {"ok": True})
        decoded = json.loads(body)
        self.assertEqual({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}, decoded)

    def test_error_envelope(self):
        body = jsonrpc_error(2, -32601, "method not found: foo")
        decoded = json.loads(body)
        self.assertEqual(
            {"jsonrpc": "2.0", "id": 2,
             "error": {"code": -32601, "message": "method not found: foo"}},
            decoded,
        )


# --- MCP handlers ----------------------------------------------------------


class TestHandleInitialize(unittest.TestCase):
    def test_returns_protocol_version_and_caps(self):
        result = handle_initialize({})
        self.assertEqual("2024-11-05", result["protocolVersion"])
        self.assertIn("tools", result["capabilities"])  # type: ignore[index]
        self.assertEqual(
            "bot-bottle-supervise",
            result["serverInfo"]["name"],  # type: ignore[index]
        )


class TestHandleToolsList(unittest.TestCase):
    def test_returns_all_tools(self):
        result = handle_tools_list({})
        names = [t["name"] for t in result["tools"]]  # type: ignore[index]
        self.assertEqual(
            sorted([
                _sv.TOOL_EGRESS_ALLOW,
                _sv.TOOL_EGRESS_BLOCK,
                _sv.TOOL_LIST_EGRESS_ROUTES,
                _sv.TOOL_CHECK_PROPOSAL,
            ]),
            sorted(names),
        )

    def test_remediation_tools_have_inputSchema_with_two_required_fields(self):
        # Only the proposal/remediation tools have required input
        # fields. The list-* introspection tools take no input.
        for tool in TOOL_DEFINITIONS:
            name = tool["name"]
            if name not in PROPOSED_FILE_FIELD:
                continue
            with self.subTest(name=name):
                schema = tool["inputSchema"]
                self.assertEqual("object", schema["type"])  # type: ignore[index]
                required = schema["required"]  # type: ignore[index]
                self.assertEqual(2, len(required))
                self.assertIn("justification", required)
                self.assertIn(PROPOSED_FILE_FIELD[name], required)  # type: ignore[index]

    def test_list_egress_routes_takes_no_input(self):
        tool = next(
            t for t in TOOL_DEFINITIONS
            if t["name"] == _sv.TOOL_LIST_EGRESS_ROUTES
        )
        schema = tool["inputSchema"]
        self.assertEqual({}, schema.get("properties"))  # type: ignore[union-attr]
        # No `required` array because no inputs are required.
        self.assertNotIn("required", schema)  # type: ignore[operator]

    def test_egress_tools_take_routes_yaml_and_justification(self):
        for tool_name in (_sv.TOOL_EGRESS_ALLOW, _sv.TOOL_EGRESS_BLOCK):
            with self.subTest(tool_name=tool_name):
                tool = next(t for t in TOOL_DEFINITIONS if t["name"] == tool_name)
                schema = tool["inputSchema"]
                self.assertEqual("object", schema["type"])  # type: ignore[index]
                self.assertEqual(
                    ["routes_yaml", "justification"],
                    schema["required"],  # type: ignore[index]
                )


class TestHandleToolsCall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-server-test.")
        self._home_patch = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        self.resolver = _FakeSuperviseResolver("dev")
        _qs.QueueStore("dev").migrate()
        _as.AuditStore().migrate()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _respond_when_proposal_appears(self, status: str, notes: str = "") -> threading.Thread:
        """Background thread: poll the queue for a fresh proposal, write a
        matching response — the operator half, out of band."""
        def runner():
            for _ in range(200):
                pending = _SV.list_pending_proposals("dev")
                if pending:
                    p = pending[0]
                    _SV.write_response("dev", _sv.Response(
                        proposal_id=p.id, status=status, notes=notes,
                    ))
                    return
                time.sleep(0.01)

        t = threading.Thread(target=runner)
        t.start()
        return t

    def test_call_round_trips_through_queue(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED, notes="lgtm")
        try:
            result = _tools_call(self.resolver, {
                "name": _sv.TOOL_EGRESS_BLOCK,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "need example.com",
                },
            })
        finally:
            responder.join()
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: approved", text)
        self.assertIn("notes: lgtm", text)

    def test_allow_round_trips_through_queue(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED, notes="ok")
        try:
            result = _tools_call(self.resolver, {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "need example.com",
                },
            })
        finally:
            responder.join()
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: approved", text)
        self.assertIn("notes: ok", text)

    def test_rejected_response_sets_isError(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_REJECTED, notes="nope")
        try:
            result = _tools_call(self.resolver, {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "needed for tests",
                },
            })
        finally:
            responder.join()
        self.assertTrue(result["isError"])  # type: ignore[index]

    def test_invalid_tool_name_raises(self):
        with self.assertRaises(_RpcError) as cm:
            _tools_call(self.resolver, {"name": "not-a-tool", "arguments": {}})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_missing_justification_raises(self):
        with self.assertRaises(_RpcError):
            _tools_call(self.resolver, {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {"routes_yaml": "routes:\n  - host: example.com\n"},
            })

    def test_missing_name_raises(self):
        with self.assertRaises(_RpcError) as cm:
            _tools_call(self.resolver, {"arguments": {}})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_arguments_must_be_object(self):
        with self.assertRaises(_RpcError) as cm:
            _tools_call(self.resolver, {"name": _sv.TOOL_EGRESS_ALLOW, "arguments": []})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("must be an object", cm.exception.message)

    def test_capability_block_call_raises_unknown_tool(self):
        with self.assertRaises(_RpcError) as cm:
            _tools_call(self.resolver, {
                "name": "capability-block",
                "arguments": {
                    "dockerfile": "FROM python:3.13\n",
                    "justification": "need git",
                },
            })
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("unknown tool", cm.exception.message)

    def test_decided_proposal_drops_off_pending(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED)
        try:
            _tools_call(self.resolver, {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "x",
                },
            })
        finally:
            responder.join()
        # A decided proposal drops off the operator's pending list (a response
        # row exists) — poll itself no longer archives (issue #469 review).
        self.assertEqual([], _SV.list_pending_proposals("dev"))

    def test_pending_response_times_out_without_archive(self):
        result = _tools_call(
            self.resolver,
            {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "need egress",
                },
            },
            ServerConfig(response_timeout_seconds=0.05),
        )
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: pending", text)
        self.assertIn("proposal remains queued", text)
        self.assertEqual(1, len(_SV.list_pending_proposals("dev")))

    _ALLOW: dict[str, object] = {
        "name": _sv.TOOL_EGRESS_ALLOW,
        "arguments": {"routes_yaml": "routes:\n  - host: example.com\n", "justification": "x"},
    }

    def test_poll_unreachable_after_queue_raises_internal(self):
        # Proposal queues, then the orchestrator becomes unreachable on poll.
        self.resolver.poll_raises = True
        with self.assertRaises(_RpcInternalError) as cm:
            _tools_call(self.resolver, self._ALLOW, ServerConfig(response_timeout_seconds=5))
        self.assertEqual(ERR_INTERNAL, cm.exception.code)

    def test_poll_unattributed_after_queue_raises_internal(self):
        # Proposal queues, then poll comes back unattributed (fail-closed).
        self.resolver.poll_none = True
        with self.assertRaises(_RpcInternalError) as cm:
            _tools_call(self.resolver, self._ALLOW, ServerConfig(response_timeout_seconds=5))
        self.assertEqual(ERR_INTERNAL, cm.exception.code)


class TestResponseTimeoutEnv(unittest.TestCase):
    def test_unset_uses_default(self):
        self.assertEqual(
            supervise_server.DEFAULT_RESPONSE_TIMEOUT_SECONDS,
            _response_timeout_from_env({}),
        )

    def test_positive_float_accepted(self):
        self.assertEqual(
            12.5,
            _response_timeout_from_env({"SUPERVISE_RESPONSE_TIMEOUT_SECONDS": "12.5"}),
        )

    def test_invalid_value_rejected(self):
        with self.assertRaises(ValueError):
            _response_timeout_from_env({"SUPERVISE_RESPONSE_TIMEOUT_SECONDS": "soon"})

    def test_nonpositive_value_rejected(self):
        with self.assertRaises(ValueError):
            _response_timeout_from_env({"SUPERVISE_RESPONSE_TIMEOUT_SECONDS": "0"})


# --- Response text formatting ---------------------------------------------


class TestFormatResponseText(unittest.TestCase):
    def test_approved_with_notes(self):
        text = format_response_text(_sv.Response(
            proposal_id="x", status=_sv.STATUS_APPROVED, notes="retry now",
        ))
        self.assertIn("status: approved", text)
        self.assertIn("notes: retry now", text)

    def test_modified_includes_modified_hint(self):
        text = format_response_text(_sv.Response(
            proposal_id="x", status=_sv.STATUS_MODIFIED, notes="",
            final_file="modified content",
        ))
        self.assertIn("status: modified", text)
        self.assertIn("the operator modified", text.lower())


class TestFormatPendingResponseText(unittest.TestCase):
    def test_formats_timeout_message(self):
        text = supervise_server.format_pending_response_text("prop-9", 12.5)
        self.assertIn("status: pending", text)
        self.assertIn("12.5s", text)
        self.assertIn("proposal_id: prop-9", text)


# --- End-to-end HTTP sanity ------------------------------------------------


class TestHttpEndToEnd(unittest.TestCase):
    """Spin up the server on a random port and round-trip a tools/list
    over real HTTP. Catches the JSON-RPC plumbing if it ever drifts
    from the unit-level handlers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-http-test.")
        # Pick a random port by binding to :0 first.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = MCPServer(("127.0.0.1", self.port), MCPHandler)
        self.server.config = ServerConfig()
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def _post_jsonrpc(self, body: dict[str, object]) -> dict[str, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = json.dumps(body).encode("utf-8")
            conn.request("POST", "/", body=payload,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(payload))})
            resp = conn.getresponse()
            data = resp.read()
            return json.loads(data)
        finally:
            conn.close()

    def test_tools_list_over_http(self):
        result = self._post_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual("2.0", result["jsonrpc"])
        self.assertEqual(1, result["id"])
        names = [t["name"] for t in result["result"]["tools"]]  # type: ignore[index]
        self.assertNotIn("capability-block", names)
        self.assertIn(_sv.TOOL_EGRESS_ALLOW, names)
        self.assertIn(_sv.TOOL_EGRESS_BLOCK, names)

    def test_unknown_method_returns_jsonrpc_error(self):
        result = self._post_jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "does/not/exist"},
        )
        self.assertEqual(ERR_METHOD_NOT_FOUND, result["error"]["code"])  # type: ignore[index]

    def test_no_resolver_fails_closed_over_http(self):
        # The test server has no policy_resolver wired, so a proposal tools/call
        # fails closed with ERR_INTERNAL rather than queuing anything.
        result = self._post_jsonrpc({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "x",
                },
            },
        })
        self.assertIn("error", result)
        self.assertEqual(ERR_INTERNAL, result["error"]["code"])  # type: ignore[index]

    def test_health_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual(b"ok\n", resp.read())
        finally:
            conn.close()


def _handler(resolver: object) -> MCPHandler:
    """A bare MCPHandler wired with a server (carrying the resolver) and a
    client address, enough to exercise the resolver-backed paths off-socket."""
    h: MCPHandler = MCPHandler.__new__(MCPHandler)
    h.server = types.SimpleNamespace(policy_resolver=resolver)  # type: ignore[assignment]
    h.client_address = ("10.0.0.7", 4321)
    h.headers = {}  # type: ignore[assignment]
    return h


class TestResolverFailClosed(unittest.TestCase):
    """A dispatch without a resolver is a misconfiguration, not a tenancy mode
    — fail closed rather than queue (or list) anything (PRD 0070)."""

    def test_missing_resolver_raises(self) -> None:
        with self.assertRaises(_RpcInternalError):
            _handler(None)._resolver_or_fail()


class TestResolvedRoutesPayload(unittest.TestCase):
    """`list-egress-routes` answers from the calling bottle's resolved policy —
    not the gateway's empty static table. Regression: an empty list led agents
    to propose replace-all route files that dropped base hosts like
    api.anthropic.com on approval."""

    def test_returns_resolved_bottle_routes(self) -> None:
        policy = (
            "routes:\n"
            "  - host: api.anthropic.com\n"
            "  - host: www.google.com\n"
        )
        payload = _handler(
            _FakeSuperviseResolver(bottle_id="b1", policy=policy)
        )._resolved_routes_payload()
        assert payload is not None
        self.assertFalse(payload["isError"])  # type: ignore[index]
        data = json.loads(payload["content"][0]["text"])  # type: ignore[index]
        hosts = {r["host"] for r in data["routes"]}
        self.assertEqual({"api.anthropic.com", "www.google.com"}, hosts)

    def test_orchestrator_error_fails_closed_to_empty(self) -> None:
        # resolve_client_context swallows resolver errors → deny-all (empty),
        # never another bottle's routes.
        payload = _handler(
            _FakeSuperviseResolver(raises=True)
        )._resolved_routes_payload()
        assert payload is not None
        data = json.loads(payload["content"][0]["text"])  # type: ignore[index]
        self.assertEqual([], data["routes"])

    def test_missing_resolver_fails_closed(self) -> None:
        # A server without a resolver is a misconfig, not a mode: raise rather
        # than list anything.
        with self.assertRaises(_RpcInternalError):
            _handler(None)._resolved_routes_payload()


class TestNonBlockingSupervise(unittest.TestCase):
    """PRD prd-new / issue #412: pending responses carry the proposal id, and
    `check-proposal` polls a queued proposal without blocking or re-proposing."""

    _ROUTES = "routes:\n  - host: example.com\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-nonblock-test.")
        self._home_patch = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")
        self.resolver = _FakeSuperviseResolver("dev")
        _qs.QueueStore("dev").migrate()
        _as.AuditStore().migrate()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _seed_proposal(self) -> "_sv.Proposal":
        p = _sv.Proposal.new(
            bottle_slug="dev",
            tool=_sv.TOOL_EGRESS_ALLOW,
            proposed_file=self._ROUTES,
            justification="need example.com",
        )
        _SV.write_proposal(p)
        return p

    # --- pending response carries the id ---

    def test_pending_text_includes_id_and_pointer(self):
        text = format_pending_response_text("abc-123", 30.0)
        self.assertIn("status: pending", text)
        self.assertIn("proposal_id: abc-123", text)
        self.assertIn("check-proposal", text)

    def test_tools_call_timeout_returns_pending_with_id_and_stays_queued(self):
        # No responder → the grace window expires → pending, not blocked forever.
        result = _tools_call(
            self.resolver,
            {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {"routes_yaml": self._ROUTES, "justification": "x"},
            },
            ServerConfig(response_timeout_seconds=0.05),
        )
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: pending", text)
        pending = _SV.list_pending_proposals("dev")
        self.assertEqual(1, len(pending))  # still queued, not archived
        self.assertIn(pending[0].id, text)  # agent got the id to poll

    # --- check-proposal poll ---

    def test_check_returns_approved_idempotently(self):
        p = self._seed_proposal()
        _SV.write_response("dev", _sv.Response(proposal_id=p.id, status=_sv.STATUS_APPROVED, notes="ok"))
        result = _check(self.resolver, {"arguments": {"proposal_id": p.id}})
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: approved", text)
        self.assertIn("notes: ok", text)
        # Poll doesn't archive, so a re-check returns the same decision
        # (issue #469 review) rather than 'unknown'.
        again = _check(self.resolver, {"arguments": {"proposal_id": p.id}})
        self.assertEqual(result, again)

    def test_check_rejected_sets_isError(self):
        p = self._seed_proposal()
        _SV.write_response("dev", _sv.Response(proposal_id=p.id, status=_sv.STATUS_REJECTED, notes="no"))
        result = _check(self.resolver, {"arguments": {"proposal_id": p.id}})
        self.assertTrue(result["isError"])
        self.assertIn("status: rejected", result["content"][0]["text"])  # type: ignore[index]

    def test_check_pending_when_no_decision_yet(self):
        p = self._seed_proposal()
        result = _check(self.resolver, {"arguments": {"proposal_id": p.id}})
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: pending", text)
        self.assertIn(p.id, text)
        self.assertEqual(1, len(_SV.list_pending_proposals("dev")))  # not archived

    def test_check_unknown_id_is_error(self):
        result = _check(self.resolver, {"arguments": {"proposal_id": "no-such-proposal"}})
        self.assertTrue(result["isError"])
        self.assertIn("status: unknown", result["content"][0]["text"])  # type: ignore[index]

    def test_check_poll_unreachable_raises_internal(self):
        self.resolver.poll_raises = True
        with self.assertRaises(_RpcInternalError) as cm:
            _check(self.resolver, {"arguments": {"proposal_id": "p"}})
        self.assertEqual(ERR_INTERNAL, cm.exception.code)

    def test_check_poll_unattributed_raises_internal(self):
        self.resolver.poll_none = True
        with self.assertRaises(_RpcInternalError) as cm:
            _check(self.resolver, {"arguments": {"proposal_id": "p"}})
        self.assertEqual(ERR_INTERNAL, cm.exception.code)

    def test_check_missing_id_raises(self):
        with self.assertRaises(_RpcClientError) as cm:
            _check(self.resolver, {"arguments": {}})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_check_empty_id_raises(self):
        with self.assertRaises(_RpcClientError) as cm:
            _check(self.resolver, {"arguments": {"proposal_id": "   "}})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_check_arguments_must_be_object(self):
        with self.assertRaises(_RpcClientError) as cm:
            _check(self.resolver, {"arguments": []})
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_full_nonblocking_round_trip(self):
        # 1. tools/call times out → pending with id
        result = _tools_call(
            self.resolver,
            {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {"routes_yaml": self._ROUTES, "justification": "x"},
            },
            ServerConfig(response_timeout_seconds=0.05),
        )
        pid = _SV.list_pending_proposals("dev")[0].id
        self.assertIn(pid, result["content"][0]["text"])  # type: ignore[index]
        # 2. operator decides out-of-band
        _SV.write_response("dev", _sv.Response(proposal_id=pid, status=_sv.STATUS_APPROVED, notes="ok"))
        # 3. agent resumes by polling — no re-proposing
        poll = _check(self.resolver, {"arguments": {"proposal_id": pid}})
        self.assertFalse(poll["isError"])
        self.assertIn("status: approved", poll["content"][0]["text"])  # type: ignore[index]
        self.assertEqual([], _SV.list_pending_proposals("dev"))  # resolved (response exists)


if __name__ == "__main__":
    unittest.main()
