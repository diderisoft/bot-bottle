"""Unit: supervise sidecar MCP server (PRD 0013)."""

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


# The server module loads `supervise` via same-directory import inside
# the container (Dockerfile.supervise WORKDIRs into /app). For tests
# we mirror that by injecting claude_bottle/ onto sys.path under the
# bare name `supervise`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_bottle"))
import supervise as _sv  # noqa: E402

from claude_bottle import supervise_server  # noqa: E402
from claude_bottle.supervise_server import (
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    MCPHandler,
    MCPServer,
    PROPOSED_FILE_FIELD,
    ServerConfig,
    TOOL_DEFINITIONS,
    _RpcError,
    format_response_text,
    handle_initialize,
    handle_tools_call,
    handle_tools_list,
    jsonrpc_error,
    jsonrpc_result,
    parse_jsonrpc,
    serve,
    validate_proposed_file,
)


# --- Validation ------------------------------------------------------------


class TestValidation(unittest.TestCase):
    def test_egress_proxy_block_requires_valid_json(self):
        with self.assertRaises(_RpcError) as cm:
            validate_proposed_file(_sv.TOOL_EGRESS_PROXY_BLOCK, "{not json")
        self.assertEqual(ERR_INVALID_PARAMS, cm.exception.code)
        self.assertIn("not valid JSON", cm.exception.message)

    def test_egress_proxy_block_requires_routes_array(self):
        with self.assertRaises(_RpcError):
            validate_proposed_file(_sv.TOOL_EGRESS_PROXY_BLOCK, '{"other": []}')

    def test_egress_proxy_block_accepts_valid_routes(self):
        validate_proposed_file(
            _sv.TOOL_EGRESS_PROXY_BLOCK,
            '{"routes": [{"path": "/x/", "upstream": "https://example.com"}]}',
        )

    def test_pipelock_block_accepts_https_url(self):
        validate_proposed_file(
            _sv.TOOL_PIPELOCK_BLOCK,
            "https://api.github.com/repos/foo/bar",
        )

    def test_pipelock_block_accepts_http_url(self):
        validate_proposed_file(
            _sv.TOOL_PIPELOCK_BLOCK,
            "http://internal.example/path/to/thing",
        )

    def test_pipelock_block_rejects_missing_scheme(self):
        with self.assertRaises(_RpcError) as cm:
            validate_proposed_file(_sv.TOOL_PIPELOCK_BLOCK, "api.github.com/foo")
        self.assertIn("http://", str(cm.exception.message))

    def test_pipelock_block_rejects_missing_host(self):
        with self.assertRaises(_RpcError) as cm:
            validate_proposed_file(_sv.TOOL_PIPELOCK_BLOCK, "https:///just-a-path")
        self.assertIn("hostname", str(cm.exception.message))

    def test_capability_block_accepts_anything_nonempty(self):
        validate_proposed_file(
            _sv.TOOL_CAPABILITY_BLOCK,
            "FROM python:3.13\nRUN apk add git\n",
        )

    def test_empty_proposed_file_rejected_for_all_tools(self):
        for tool in _sv.TOOLS:
            with self.subTest(tool=tool):
                with self.assertRaises(_RpcError):
                    validate_proposed_file(tool, "   \n\t")


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
            "claude-bottle-supervise",
            result["serverInfo"]["name"],  # type: ignore[index]
        )


class TestHandleToolsList(unittest.TestCase):
    def test_returns_all_tools(self):
        result = handle_tools_list({})
        names = [t["name"] for t in result["tools"]]  # type: ignore[index]
        self.assertEqual(
            sorted([
                _sv.TOOL_EGRESS_PROXY_BLOCK,
                _sv.TOOL_PIPELOCK_BLOCK,
                _sv.TOOL_CAPABILITY_BLOCK,
                _sv.TOOL_LIST_EGRESS_PROXY_ROUTES,
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

    def test_list_egress_proxy_routes_takes_no_input(self):
        tool = next(
            t for t in TOOL_DEFINITIONS
            if t["name"] == _sv.TOOL_LIST_EGRESS_PROXY_ROUTES
        )
        schema = tool["inputSchema"]
        self.assertEqual({}, schema.get("properties"))  # type: ignore[union-attr]
        # No `required` array because no inputs are required.
        self.assertNotIn("required", schema)  # type: ignore[operator]


class TestHandleToolsCall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-server-test.")
        self.queue_dir = Path(self._tmp.name)
        self.config = ServerConfig(bottle_slug="dev", queue_dir=self.queue_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _respond_when_proposal_appears(self, status: str, notes: str = "") -> threading.Thread:
        """Background thread: poll the queue for a fresh proposal, write a
        matching response. Returns the thread so the test can join it."""
        def runner():
            for _ in range(200):
                pending = _sv.list_pending_proposals(self.queue_dir)
                if pending:
                    p = pending[0]
                    _sv.write_response(self.queue_dir, _sv.Response(
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
                    "name": _sv.TOOL_EGRESS_PROXY_BLOCK,
                    "arguments": {
                        "routes": '{"routes": []}',
                        "justification": "need a route",
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

    def test_rejected_response_sets_isError(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_REJECTED, notes="nope")
        try:
            result = handle_tools_call(
                {
                    "name": _sv.TOOL_PIPELOCK_BLOCK,
                    "arguments": {
                        "failed_url": "https://example.com/path",
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
                    "name": _sv.TOOL_EGRESS_PROXY_BLOCK,
                    "arguments": {"routes": '{"routes": []}'},
                },
                self.config,
            )

    def test_archives_proposal_after_response(self):
        responder = self._respond_when_proposal_appears(_sv.STATUS_APPROVED)
        try:
            handle_tools_call(
                {
                    "name": _sv.TOOL_EGRESS_PROXY_BLOCK,
                    "arguments": {
                        "routes": '{"routes": []}',
                        "justification": "x",
                    },
                },
                self.config,
            )
        finally:
            responder.join()
        # No pending proposals left after archive.
        self.assertEqual([], _sv.list_pending_proposals(self.queue_dir))
        # Both files moved to processed/.
        processed = list((self.queue_dir / "processed").glob("*.json"))
        self.assertEqual(2, len(processed))


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


# --- End-to-end HTTP sanity ------------------------------------------------


class TestHttpEndToEnd(unittest.TestCase):
    """Spin up the server on a random port and round-trip a tools/list
    over real HTTP. Catches the JSON-RPC plumbing if it ever drifts
    from the unit-level handlers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-http-test.")
        self.queue_dir = Path(self._tmp.name)
        # Pick a random port by binding to :0 first.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = MCPServer(("127.0.0.1", self.port), MCPHandler)
        self.server.config = ServerConfig(bottle_slug="dev", queue_dir=self.queue_dir)
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
        self.assertIn(_sv.TOOL_EGRESS_PROXY_BLOCK, names)

    def test_unknown_method_returns_jsonrpc_error(self):
        result = self._post_jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "does/not/exist"},
        )
        self.assertEqual(ERR_METHOD_NOT_FOUND, result["error"]["code"])  # type: ignore[index]

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
