"""Unit: supervise sidecar MCP server (PRD 0013)."""

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


# The server module loads `supervise` via same-directory import inside
# the container (Dockerfile.supervise WORKDIRs into /app). For tests
# we mirror that by injecting bot_bottle/ onto sys.path under the
# bare name `supervise`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bot_bottle"))
import supervise as _sv  # noqa: E402  # type: ignore
import queue_store as _qs  # noqa: E402  # type: ignore
import audit_store as _as  # noqa: E402  # type: ignore
import _supervise_types as _svt_flat  # noqa: E402  # type: ignore

from bot_bottle import supervise_server  # noqa: E402
from bot_bottle import _supervise_types as _svt_pkg  # noqa: E402
from bot_bottle.supervise_server import (
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
    format_response_text,
    handle_initialize,
    handle_list_egress_routes,
    handle_tools_call,
    handle_tools_list,
    jsonrpc_error,
    jsonrpc_result,
    parse_jsonrpc,
    validate_proposed_file,
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
        config = ServerConfig(bottle_slug="dev")
        with self.assertRaises(_RpcClientError) as cm:
            handle_tools_call({"name": "no-such-tool", "arguments": {}}, config)
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)


class TestRpcInternalErrorOnIoFailure(unittest.TestCase):
    def test_write_proposal_os_error_raises_internal(self):
        config = ServerConfig(
            bottle_slug="dev",
        )
        with patch.object(_sv, "write_proposal", side_effect=OSError("disk full")), \
                self.assertRaises(_RpcInternalError) as cm:
            handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": {
                        "routes_yaml": "routes:\n  - host: example.com\n",
                        "justification": "x",
                    },
                },
                config,
            )
        self.assertEqual(ERR_INTERNAL, cm.exception.code)
        self.assertIsNotNone(cm.exception.__cause__)


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
        self._home_patch = self._patch_home(Path(self._tmp.name))
        self.config = ServerConfig(bottle_slug="dev")
        _qs.QueueStore("dev").migrate()
        _as.AuditStore().migrate()

    def tearDown(self):
        self._home_patch()
        self._tmp.cleanup()

    def _patch_home(self, fake_home: Path):
        original_sv = _sv.bot_bottle_root
        original_flat = _svt_flat.bot_bottle_root
        original_pkg = _svt_pkg.bot_bottle_root

        def fake_root() -> Path:
            return fake_home / ".bot-bottle"

        _sv.bot_bottle_root = fake_root  # type: ignore[assignment]
        _svt_flat.bot_bottle_root = fake_root  # type: ignore[assignment]
        _svt_pkg.bot_bottle_root = fake_root  # type: ignore[assignment]

        def restore() -> None:
            _sv.bot_bottle_root = original_sv  # type: ignore[assignment]
            _svt_flat.bot_bottle_root = original_flat  # type: ignore[assignment]
            _svt_pkg.bot_bottle_root = original_pkg  # type: ignore[assignment]

        return restore

    def _respond_when_proposal_appears(self, status: str, notes: str = "") -> threading.Thread:
        """Background thread: poll the queue for a fresh proposal, write a
        matching response. Returns the thread so the test can join it."""
        def runner():
            for _ in range(200):
                pending = _sv.list_pending_proposals("dev")
                if pending:
                    p = pending[0]
                    _sv.write_response("dev", _sv.Response(
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
            result = handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_BLOCK,
                    "arguments": {
                        "routes_yaml": "routes:\n  - host: example.com\n",
                        "justification": "need example.com",
                    },
                },
                self.config,
            )
        finally:
            responder.join()
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: approved", text)
        self.assertIn("notes: lgtm", text)

    def test_allow_round_trips_through_queue(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED, notes="ok")
        try:
            result = handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": {
                        "routes_yaml": "routes:\n  - host: example.com\n",
                        "justification": "need example.com",
                    },
                },
                self.config,
            )
        finally:
            responder.join()
        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: approved", text)
        self.assertIn("notes: ok", text)

    def test_rejected_response_sets_isError(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_REJECTED, notes="nope")
        try:
            result = handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": {
                        "routes_yaml": "routes:\n  - host: example.com\n",
                        "justification": "needed for tests",
                    },
                },
                self.config,
            )
        finally:
            responder.join()
        self.assertTrue(result["isError"])  # type: ignore[index]

    def test_invalid_tool_name_raises(self):
        with self.assertRaises(_RpcError) as cm:
            handle_tools_call(
                {"name": "not-a-tool", "arguments": {}},
                self.config,
            )
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_missing_justification_raises(self):
        with self.assertRaises(_RpcError):
            handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": {"routes_yaml": "routes:\n  - host: example.com\n"},
                },
                self.config,
            )

    def test_missing_name_raises(self):
        with self.assertRaises(_RpcError) as cm:
            handle_tools_call({"arguments": {}}, self.config)
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)

    def test_arguments_must_be_object(self):
        with self.assertRaises(_RpcError) as cm:
            handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": [],
                },
                self.config,
            )
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("must be an object", cm.exception.message)

    def test_capability_block_call_raises_unknown_tool(self):
        with self.assertRaises(_RpcError) as cm:
            handle_tools_call(
                {
                    "name": "capability-block",
                    "arguments": {
                        "dockerfile": "FROM python:3.13\n",
                        "justification": "need git",
                    },
                },
                self.config,
            )
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("unknown tool", cm.exception.message)

    def test_archives_proposal_after_response(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED)
        try:
            handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_ALLOW,
                    "arguments": {
                        "routes_yaml": "routes:\n  - host: example.com\n",
                        "justification": "x",
                    },
                },
                self.config,
            )
        finally:
            responder.join()
        # No pending proposals left after archive.
        self.assertEqual([], _sv.list_pending_proposals("dev"))

    def test_pending_response_times_out_without_archive(self):
        config = ServerConfig(
            bottle_slug="dev",
            response_timeout_seconds=0.05,
        )
        result = handle_tools_call(
            {
                "name": _sv.TOOL_EGRESS_ALLOW,
                "arguments": {
                    "routes_yaml": "routes:\n  - host: example.com\n",
                    "justification": "need egress",
                },
            },
            config,
        )

        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("status: pending", text)
        self.assertIn("proposal remains queued", text)
        self.assertEqual(1, len(_sv.list_pending_proposals("dev")))


class TestHandleListEgressRoutes(unittest.TestCase):
    def test_success_returns_body_text(self):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
                return False

            def read(self):
                return b"[{\"host\": \"example.com\"}]"

        class _Opener:
            def open(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003  # type: ignore
                return _Resp()

        with patch.object(supervise_server.urllib.request, "build_opener", return_value=_Opener()):
            result = handle_list_egress_routes(
                {},
                ServerConfig(bottle_slug="dev"),
            )

        self.assertFalse(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("example.com", text)

    def test_url_error_returns_tool_error(self):
        class _Opener:
            def open(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003  # type: ignore
                raise OSError("egress unavailable")

        with patch.object(supervise_server.urllib.request, "build_opener", return_value=_Opener()):
            result = handle_list_egress_routes(
                {},
                ServerConfig(bottle_slug="dev"),
            )

        self.assertTrue(result["isError"])  # type: ignore[index]
        text = result["content"][0]["text"]  # type: ignore[index]
        self.assertIn("could not reach", text)
        self.assertIn("egress unavailable", text)


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
        text = supervise_server.format_pending_response_text(12.5)
        self.assertIn("status: pending", text)
        self.assertIn("12.5s", text)


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
        self.server.config = ServerConfig(bottle_slug="dev")
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

    def test_internal_error_returns_err_internal_over_http(self):
        with patch.object(
            supervise_server._sv, "write_proposal",
            side_effect=OSError("disk full"),
        ):
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


if __name__ == "__main__":
    unittest.main()
