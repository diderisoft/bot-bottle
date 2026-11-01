"""Unit tests for the orchestrator HTTP control plane (PRD 0070).

Mostly exercises the pure `dispatch()` (socket-free, like the supervise
server tests), plus one real-socket round-trip to prove the handler wiring.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import httpx
from contextlib import closing
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from bot_bottle.contract.auth import ROLE_CLI, ROLE_GATEWAY, mint
from bot_bottle.orchestrator import api as orchestrator_api
from bot_bottle.contract.http import ORCHESTRATOR_AUTH_HEADER
from bot_bottle.orchestrator.hostctl_relay import LiveBottlesUnknown
from bot_bottle.orchestrator.server import MAX_BODY_BYTES, create_app, make_server
from bot_bottle.orchestrator.store.registry_store import BottleRecord, RegistryStore
from bot_bottle.orchestrator.service import OrchestratorCore
from bot_bottle.orchestrator.store.store_manager import StoreManager
from bot_bottle.contract.supervise import (
    Proposal,
    TOOL_EGRESS_ALLOW,
)
from bot_bottle.orchestrator.supervisor import Supervisor


def _body(obj: object) -> bytes:
    return json.dumps(obj).encode()


def dispatch(
    orchestrator: OrchestratorCore,
    method: str,
    path: str,
    body: bytes,
    *,
    role: str | None = ROLE_CLI,
    hostctl: Any = None,
) -> tuple[int, dict[str, object]]:
    """Exercise the real ASGI application without a network socket."""
    key = "in-process-dispatch-key"
    headers = {"content-type": "application/json"}
    if role is not None:
        headers[ORCHESTRATOR_AUTH_HEADER] = mint(role, key)

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=create_app(orchestrator, signing_key=key, hostctl=hostctl))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://orchestrator",
            follow_redirects=True,
        ) as client:
            return await client.request(
                method, path, content=body, headers=headers,
            )

    response = asyncio.run(request())
    payload = response.json()
    if isinstance(payload, dict) and "detail" in payload and "error" not in payload:
        payload = {"error": payload["detail"]}
    return response.status_code, payload


def _orchestrator(db_path: Path) -> OrchestratorCore:
    store = RegistryStore(db_path)
    store.migrate()
    StoreManager(db_path).migrate()
    return OrchestratorCore(store)


class TestDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_health(self) -> None:
        status, payload = dispatch(self.orch, "GET", "/health", b"")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_register_returns_id_and_token(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"})
        )
        self.assertEqual(201, status)
        self.assertTrue(payload["bottle_id"])
        self.assertTrue(payload["identity_token"])

    def test_register_and_reprovision_encrypted_tokens(self) -> None:
        key = base64.urlsafe_b64encode(b"unit-test-key").rstrip(b"=").decode()
        status, payload = dispatch(
            self.orch, "POST", "/bottles", _body({
                "source_ip": "10.243.0.11",
                "tokens": {"EGRESS_TOKEN_0": "upstream-secret"},
                "env_var_secret": key,
            }),
        )
        self.assertEqual(201, status)
        bottle_id = payload["bottle_id"]
        assert isinstance(bottle_id, str)
        self.orch._tokens.clear()
        status, response = dispatch(
            self.orch, "POST", f"/bottles/{bottle_id}/reprovision_gateway",
            _body({"env_var_secret": key}),
        )
        self.assertEqual((200, {"reprovisioned": True}), (status, response))
        self.assertEqual(
            {"EGRESS_TOKEN_0": "upstream-secret"}, self.orch.tokens_for(bottle_id),
        )

    def test_update_agent_secret_in_place(self) -> None:
        key = base64.urlsafe_b64encode(b"unit-test-key").rstrip(b"=").decode()
        status, payload = dispatch(
            self.orch, "POST", "/bottles", _body({
                "source_ip": "10.243.0.21",
                "tokens": {"A": "old", "B": "keep"},
                "env_var_secret": key,
            }),
        )
        self.assertEqual(201, status)
        bottle_id = payload["bottle_id"]
        assert isinstance(bottle_id, str)
        status, response = dispatch(
            self.orch, "POST", f"/bottles/{bottle_id}/secret",
            _body({"name": "A", "value": "fresh", "env_var_secret": key}),
        )
        self.assertEqual((200, {"updated": True}), (status, response))
        self.assertEqual({"A": "fresh", "B": "keep"}, self.orch.tokens_for(bottle_id))

    def test_update_agent_secret_validates_and_unknown_bottle(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/bottles/b1/secret", b"not-json")
        self.assertEqual(400, status)
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/secret", _body({"name": "A"}),
        )
        self.assertEqual(400, status)  # value + env_var_secret missing
        status, _ = dispatch(
            self.orch, "POST", "/bottles/ghost/secret",
            _body({"name": "A", "value": "v", "env_var_secret": "k"}),
        )
        self.assertEqual(404, status)

    def test_update_agent_secret_is_cli_only(self) -> None:
        # A data-plane (`gateway`) caller must not set a bottle's tokens.
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/secret",
            _body({"name": "A", "value": "v", "env_var_secret": "k"}),
            role=ROLE_GATEWAY,
        )
        self.assertEqual(403, status)

    def test_reprovision_validates_request_and_missing_rows(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway", b"not-json",
        )
        self.assertEqual(400, status)
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway", _body({}),
        )
        self.assertEqual(400, status)
        status, _ = dispatch(
            self.orch, "POST", "/bottles/b1/reprovision_gateway",
            _body({"env_var_secret": "key"}),
        )
        self.assertEqual(404, status)

    def test_register_requires_source_ip(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/bottles", _body({}))
        self.assertEqual(400, status)

    def test_register_rejects_bad_json(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/bottles", b"{not json")
        self.assertEqual(400, status)

    def test_list_redacts_identity_token(self) -> None:
        dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        status, payload = dispatch(self.orch, "GET", "/bottles", b"")
        self.assertEqual(200, status)
        bottles = payload["bottles"]
        assert isinstance(bottles, list)
        self.assertEqual(1, len(bottles))
        first = bottles[0]
        assert isinstance(first, dict)
        self.assertNotIn("identity_token", first)
        self.assertEqual("10.243.0.1", first["source_ip"])

    def test_attribute_ok_and_forbidden(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.5"})
        )
        token = reg["identity_token"]
        ok_status, ok = dispatch(
            self.orch, "POST", "/attribute",
            _body({"source_ip": "10.243.0.5", "identity_token": token}),
        )
        self.assertEqual(200, ok_status)
        self.assertEqual(reg["bottle_id"], ok["bottle_id"])

        bad_status, _ = dispatch(
            self.orch, "POST", "/attribute",
            _body({"source_ip": "10.243.0.5", "identity_token": "nope"}),
        )
        self.assertEqual(403, bad_status)

    def test_attribute_requires_both_fields(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/attribute", _body({"source_ip": "10.243.0.5"})
        )
        self.assertEqual(400, status)

    def test_delete(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"})
        )
        bid = reg["bottle_id"]
        status, payload = dispatch(self.orch, "DELETE", f"/bottles/{bid}", b"")
        self.assertEqual(200, status)
        self.assertEqual(True, payload["torn_down"])
        self.assertEqual([], self.orch.registry.all())

    def test_delete_missing_404(self) -> None:
        status, _ = dispatch(self.orch, "DELETE", "/bottles/ghost", b"")
        self.assertEqual(404, status)

    def test_unknown_route_404(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/nope", b"")
        self.assertEqual(404, status)

    def test_trailing_slash_normalized(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/health/", b"")
        self.assertEqual(200, status)

    def test_gateway_status_unconfigured(self) -> None:
        status, payload = dispatch(self.orch, "GET", "/gateway", b"")
        self.assertEqual(200, status)
        self.assertEqual(False, payload["configured"])

    def test_launch_stores_policy_and_resolve_returns_it(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.5", "policy": '{"a":1}'}),
        )
        status, payload = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.5", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual(200, status)
        self.assertEqual(reg["bottle_id"], payload["bottle_id"])
        self.assertEqual('{"a":1}', payload["policy"])

    def test_resolve_forbidden_on_bad_token(self) -> None:
        dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.5"}))
        status, _ = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.5", "identity_token": "nope"}),
        )
        self.assertEqual(403, status)

    def test_put_policy_updates_live(self) -> None:
        _, reg = dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        bid = reg["bottle_id"]
        status, payload = dispatch(
            self.orch, "PUT", f"/bottles/{bid}/policy", _body({"policy": '{"v":9}'})
        )
        self.assertEqual(200, status)
        self.assertEqual(True, payload["updated"])
        _, resolved = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.1", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual('{"v":9}', resolved["policy"])

    def test_put_policy_missing_bottle_404(self) -> None:
        status, _ = dispatch(
            self.orch, "PUT", "/bottles/ghost/policy", _body({"policy": "{}"})
        )
        self.assertEqual(404, status)

    def test_put_policy_requires_string(self) -> None:
        _, reg = dispatch(self.orch, "POST", "/bottles", _body({"source_ip": "10.243.0.1"}))
        status, _ = dispatch(
            self.orch, "PUT", f"/bottles/{reg['bottle_id']}/policy", _body({})
        )
        self.assertEqual(400, status)

    def test_resolve_without_token_denies(self) -> None:
        # Mandatory token: source-IP alone no longer resolves (fail-closed 403).
        dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.5", "policy": "P"}),
        )
        status, _ = dispatch(
            self.orch, "POST", "/resolve", _body({"source_ip": "10.243.0.5"})
        )
        self.assertEqual(403, status)

    def test_resolve_with_matching_token(self) -> None:
        _, reg = dispatch(
            self.orch, "POST", "/bottles",
            _body({"source_ip": "10.243.0.6", "policy": "P"}),
        )
        status, payload = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.243.0.6", "identity_token": reg["identity_token"]}),
        )
        self.assertEqual(200, status)
        self.assertEqual(reg["bottle_id"], payload["bottle_id"])
        self.assertEqual("P", payload["policy"])

    def test_resolve_requires_source_ip(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/resolve", _body({}))
        self.assertEqual(400, status)


class TestVersionRoute(unittest.TestCase):
    """GET /version — public, returns version/commit/protocol/images from env vars."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_version_is_public(self) -> None:
        env = {"BOT_BOTTLE_VERSION": "1.2.3", "BOT_BOTTLE_COMMIT": "abc",
               "BOT_BOTTLE_PROTOCOL": "1"}
        with patch.dict("os.environ", env):
            status, payload = dispatch(self.orch, "GET", "/version", b"", role=None)
        self.assertEqual(200, status)

    def test_version_shape(self) -> None:
        env = {"BOT_BOTTLE_VERSION": "0.1.6", "BOT_BOTTLE_COMMIT": "abc123",
               "BOT_BOTTLE_PROTOCOL": "1"}
        with patch.dict("os.environ", env):
            _, payload = dispatch(self.orch, "GET", "/version", b"")
        self.assertEqual("0.1.6", payload["version"])
        self.assertEqual("abc123", payload["commit"])
        self.assertEqual(1, payload["protocol"])
        self.assertIsInstance(payload["images"], dict)

    def test_local_build_shows_source_and_inputs_hash(self) -> None:
        hash_val = "a1b2c3d4e5f60718"
        env = {
            "BOT_BOTTLE_VERSION": "0.1.6", "BOT_BOTTLE_COMMIT": "sha",
            "BOT_BOTTLE_PROTOCOL": "1",
            "BOT_BOTTLE_IMAGE_ORCHESTRATOR_SOURCE": "local",
            "BOT_BOTTLE_IMAGE_ORCHESTRATOR_INPUTS_HASH": hash_val,
            "BOT_BOTTLE_IMAGE_GATEWAY_SOURCE": "local",
            "BOT_BOTTLE_IMAGE_GATEWAY_INPUTS_HASH": hash_val,
        }
        with patch.dict("os.environ", env):
            _, payload = dispatch(self.orch, "GET", "/version", b"")
        for image in ("bot-bottle-orchestrator", "bot-bottle-gateway"):
            self.assertEqual({"source": "local", "inputs_hash": hash_val},
                             payload["images"][image])

    def test_published_build_shows_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        hash_val = "deadbeefcafe0011"
        env = {
            "BOT_BOTTLE_VERSION": "0.1.6", "BOT_BOTTLE_COMMIT": "",
            "BOT_BOTTLE_PROTOCOL": "1",
            "BOT_BOTTLE_IMAGE_ORCHESTRATOR_SOURCE": "published",
            "BOT_BOTTLE_IMAGE_ORCHESTRATOR_DIGEST": digest,
            "BOT_BOTTLE_IMAGE_ORCHESTRATOR_INPUTS_HASH": hash_val,
        }
        with patch.dict("os.environ", env):
            _, payload = dispatch(self.orch, "GET", "/version", b"")
        self.assertEqual(
            {"source": "published", "digest": digest, "inputs_hash": hash_val},
            payload["images"]["bot-bottle-orchestrator"],
        )

    def test_no_env_vars_returns_unknown(self) -> None:
        # Strip all BOT_BOTTLE_* vars to simulate an orchestrator started without them.
        clean = {k: v for k, v in __import__("os").environ.items()
                 if not k.startswith("BOT_BOTTLE_")}
        with patch.dict("os.environ", clean, clear=True):
            _, payload = dispatch(self.orch, "GET", "/version", b"")
        self.assertEqual("unknown", payload["version"])
        self.assertEqual(0, payload["protocol"])
        self.assertEqual({}, payload["images"])


class TestServerRoundTrip(unittest.TestCase):
    def _raw_status(self, content_length: str, *, authenticated: bool = True) -> int:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key = "request-limits-key"
        server = make_server(
            _orchestrator(Path(tmp.name) / "r.db"),
            "127.0.0.1", 0, signing_key=key,
        )
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection(
            str(server.server_address[0]), server.server_address[1], timeout=5,
        )
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/bottles")
        conn.putheader("Content-Length", content_length)
        if authenticated:
            conn.putheader(
                "x-bot-bottle-orchestrator-auth", mint(ROLE_CLI, key),
            )
        conn.endheaders()
        return conn.getresponse().status

    def test_rejects_malformed_content_length(self) -> None:
        self.assertEqual(400, self._raw_status("not-a-number"))

    def test_rejects_oversized_body_without_reading_it(self) -> None:
        self.assertEqual(413, self._raw_status(str(MAX_BODY_BYTES + 1)))

    def test_rejects_unauthenticated_request_before_reading_body(self) -> None:
        self.assertEqual(
            401,
            self._raw_status(str(MAX_BODY_BYTES), authenticated=False),
        )

    def test_http_register_health_attribute(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orch = _orchestrator(Path(tmp.name) / "r.db")
        signing_key = "round-trip-key"
        auth = mint(ROLE_CLI, signing_key)
        server = make_server(
            orch, "127.0.0.1", 0, signing_key=signing_key,
        )
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        host, port = server.server_address[0], server.server_address[1]
        base = f"http://{host}:{port}"

        reg = json.load(urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/bottles", data=_body({"source_ip": "10.243.0.7"}),
                method="POST", headers={
                    "Content-Type": "application/json",
                    "x-bot-bottle-orchestrator-auth": auth,
                },
            ), timeout=5,
        ))
        self.assertTrue(reg["bottle_id"])

        health = json.load(urllib.request.urlopen(f"{base}/health", timeout=5))
        self.assertEqual("ok", health["status"])

        attr = json.load(urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/attribute",
                data=_body({"source_ip": "10.243.0.7", "identity_token": reg["identity_token"]}),
                method="POST", headers={
                    "Content-Type": "application/json",
                    "x-bot-bottle-orchestrator-auth": auth,
                },
            ), timeout=5,
        ))
        self.assertEqual(reg["bottle_id"], attr["bottle_id"])

    def test_internal_failure_is_contextual_but_redacted(self) -> None:
        orch = MagicMock()
        orch.registry.all.side_effect = RuntimeError("SENSITIVE request value")
        signing_key = "failure-path-key"
        auth = mint(ROLE_CLI, signing_key)
        with patch("sys.stderr", io.StringIO()) as stderr:
            server = make_server(
                orch, "127.0.0.1", 0, signing_key=signing_key,
            )
            self.addCleanup(server.server_close)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            host, port = server.server_address[0], server.server_address[1]
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://{host}:{port}/bottles",
                        headers={"x-bot-bottle-orchestrator-auth": auth},
                    ),
                    timeout=5,
                )
            payload = json.loads(raised.exception.read())
            output = stderr.getvalue()
        self.assertEqual({"error": "internal error"}, payload)
        self.assertIn("GET /bottles", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("SENSITIVE", output)


class TestControlPlaneBoundary(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _scope(key: str) -> dict[str, object]:
        return {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/bottles",
            "raw_path": b"/bottles",
            "query_string": b"",
            "headers": [(
                ORCHESTRATOR_AUTH_HEADER.encode(),
                mint(ROLE_CLI, key).encode(),
            )],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "state": {},
        }

    async def test_chunked_oversized_body_returns_413(self) -> None:
        key = "stream-limit-key"
        called = False
        first: dict[str, object] = {
            "type": "http.request",
            "body": b"x" * MAX_BODY_BYTES,
            "more_body": True,
        }
        last: dict[str, object] = {
            "type": "http.request", "body": b"x", "more_body": False,
        }
        chunks: Iterator[dict[str, object]] = iter([first, last])
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return next(chunks)

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        async def inner(*_args: object) -> None:
            nonlocal called
            called = True

        boundary = orchestrator_api.ControlPlaneBoundary(inner, key)
        await boundary(self._scope(key), receive, send)  # type: ignore[arg-type]
        self.assertFalse(called)
        self.assertEqual(413, sent[0]["status"])

    async def test_slow_stream_returns_408(self) -> None:
        key = "stream-timeout-key"
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        async def inner(*_args: object) -> None:
            self.fail("timed-out body reached the application")

        boundary = orchestrator_api.ControlPlaneBoundary(inner, key)
        with patch.object(orchestrator_api, "REQUEST_BODY_TIMEOUT_SECONDS", 0.01):
            await boundary(self._scope(key), receive, send)  # type: ignore[arg-type]
        self.assertEqual(408, sent[0]["status"])


class TestOrchestratorAuth(unittest.TestCase):
    """Role-scoped control-plane tokens (issue #400 / #469 review): every route
    but /health needs a valid token, and the token's role gates which routes it
    reaches — a `gateway` data-plane token can't drive the operator routes."""

    _OPERATOR_ROUTES = [
        ("GET", "/bottles", b""),
        ("POST", "/bottles", _body({"source_ip": "10.0.0.1"})),
        ("PUT", "/bottles/x/policy", _body({"policy": "routes: []"})),
        ("DELETE", "/bottles/x", b""),
        ("POST", "/reconcile", _body({})),
        ("POST", "/attribute", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
        ("GET", "/supervise/proposals", b""),
        ("POST", "/supervise/respond",
         _body({"proposal_id": "p", "bottle_slug": "s", "decision": "a"})),
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def test_health_is_public_even_unauthenticated(self) -> None:
        status, _ = dispatch(self.orch, "GET", "/health", b"", role=None)
        self.assertEqual(200, status)

    def test_version_is_public_even_unauthenticated(self) -> None:
        with patch.dict("os.environ", {"BOT_BOTTLE_VERSION": "0.1.6"}):
            status, _ = dispatch(self.orch, "GET", "/version", b"", role=None)
        self.assertEqual(200, status)

    def test_unauthenticated_denies_every_other_route(self) -> None:
        routes = self._OPERATOR_ROUTES + [
            ("POST", "/resolve", _body({"source_ip": "10.0.0.1", "identity_token": "t"})),
            ("POST", "/supervise/propose",
             _body({"source_ip": "1", "tool": "egress-allow", "proposed_file": "x", "justification": "j"})),
            ("POST", "/supervise/poll", _body({"source_ip": "1", "proposal_id": "p"})),
        ]
        for method, path, body in routes:
            status, _ = dispatch(self.orch, method, path, body, role=None)
            self.assertEqual(401, status, f"{method} {path} should be 401 unauthenticated")

    def test_gateway_role_denied_on_operator_routes(self) -> None:
        # A compromised data-plane process holds only a `gateway` token — it must
        # not reach the operator routes (approve proposals, rewrite policy, …).
        for method, path, body in self._OPERATOR_ROUTES:
            status, payload = dispatch(self.orch, method, path, body, role=ROLE_GATEWAY)
            self.assertEqual(403, status, f"{method} {path} should be 403 for gateway")
            self.assertIn("insufficient role", str(payload.get("error")))

    def test_gateway_role_allowed_on_data_routes(self) -> None:
        # The gateway CAN reach its own lookups. Register a bottle so /resolve
        # returns 200 rather than a 403 — proving the role gate let it through.
        rec = self.orch.registry.register("10.0.0.9", policy="routes: []", metadata="")
        status, _ = dispatch(
            self.orch, "POST", "/resolve",
            _body({"source_ip": "10.0.0.9", "identity_token": rec.identity_token}),
            role=ROLE_GATEWAY)
        self.assertEqual(200, status)

    def test_deny_happens_before_the_registry_is_touched(self) -> None:
        """An unauthenticated DELETE must not tear a bottle down. 401, and the
        bottle is still there."""
        rec = self.orch.registry.register("10.0.0.9", policy="", metadata="")
        status, _ = dispatch(
            self.orch, "DELETE", f"/bottles/{rec.bottle_id}", b"", role=None)
        self.assertEqual(401, status)
        self.assertIsNotNone(self.orch.registry.get(rec.bottle_id))

    def _server_with_key(self, signing_key: str):
        server = make_server(
            self.orch, "127.0.0.1", 0, signing_key=signing_key,
        )
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        return f"http://{host}:{port}"

    def _status(self, url: str, *, header: str | None = None) -> int:
        req = urllib.request.Request(url)
        if header is not None:
            req.add_header("x-bot-bottle-orchestrator-auth", header)
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    def test_configured_server_enforces_roles_over_http(self) -> None:
        key = "test-key"
        base = self._server_with_key(key)
        gateway_tok = mint(ROLE_GATEWAY, key)
        cli_tok = mint(ROLE_CLI, key)
        # /health is public — no header needed.
        self.assertEqual(200, self._status(f"{base}/health"))
        # /bottles (operator) needs a valid cli token.
        self.assertEqual(401, self._status(f"{base}/bottles"))
        self.assertEqual(401, self._status(f"{base}/bottles", header="wrong"))
        self.assertEqual(403, self._status(f"{base}/bottles", header=gateway_tok))
        self.assertEqual(200, self._status(f"{base}/bottles", header=cli_tok))

    def test_unconfigured_server_refuses_to_start(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "signing key is required"):
                make_server(self.orch, "127.0.0.1", 0)


class TestDispatchSupervise(unittest.TestCase):
    """The /supervise/* routes over the pure dispatch()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        db = root / "db" / "bot-bottle.db"
        db.parent.mkdir(parents=True)
        self._env = patch.dict("os.environ", {
            "BOT_BOTTLE_ROOT": str(root),
        })
        self._env.start()
        self.db = db
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        self.orch = OrchestratorCore(self.store)
        self.bottle_id = ""

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _queue(self, slug: str, proposed: str) -> str:
        rec = self.store.register(
            "10.243.0.1", metadata=json.dumps({"slug": slug}), policy="routes: []\n")
        self.bottle_id = rec.bottle_id
        p = Proposal.new(
            bottle_slug=rec.bottle_id, tool=TOOL_EGRESS_ALLOW, proposed_file=proposed,
            justification="need it")
        Supervisor(self.db).write_proposal(p)
        return p.id

    def test_list_pending(self) -> None:
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        status, payload = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual(200, status)
        proposals = payload["proposals"]
        assert isinstance(proposals, list)
        self.assertEqual(pid, proposals[0]["id"])

    def test_respond_approve_applies_and_clears(self) -> None:
        pid = self._queue("demo", "routes:\n  - host: google.com\n")
        status, payload = dispatch(
            self.orch, "POST", "/supervise/respond",
            _body({"proposal_id": pid, "bottle_slug": self.bottle_id, "decision": "approve"}),
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["responded"])
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual([], listing["proposals"])

    def test_respond_requires_fields(self) -> None:
        status, _ = dispatch(
            self.orch, "POST", "/supervise/respond", _body({"decision": "approve"}))
        self.assertEqual(400, status)

    def test_respond_unknown_proposal_conflicts(self) -> None:
        status, payload = dispatch(
            self.orch, "POST", "/supervise/respond",
            _body({"proposal_id": "ghost", "bottle_slug": "demo", "decision": "approve"}),
        )
        self.assertEqual(409, status)
        self.assertIn("no such proposal", str(payload["error"]))


class TestDispatchSuperviseAgentRpc(unittest.TestCase):
    """The agent half — `/supervise/propose` + `/supervise/poll` — attributed by
    (source_ip, identity_token) like /resolve, so a bottle can only ever queue
    or read its own proposals (PRD 0070 / issue #469)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        db = root / "db" / "bot-bottle.db"
        db.parent.mkdir(parents=True)
        self._env = patch.dict("os.environ", {
            "BOT_BOTTLE_ROOT": str(root),
        })
        self._env.start()
        self.db = db
        self.store = RegistryStore(db)
        self.store.migrate()
        StoreManager(db).migrate()
        self.orch = OrchestratorCore(self.store)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _register(self, source_ip: str = "10.243.0.9", slug: str = "demo"):
        return self.store.register(
            source_ip, metadata=json.dumps({"slug": slug}), policy="routes: []\n")

    def _propose(self, rec: BottleRecord, proposed: str = "routes:\n  - host: g.com\n"):
        return dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": proposed, "justification": "need it",
        }))

    def _poll(self, rec: BottleRecord, proposal_id: str):
        return dispatch(self.orch, "POST", "/supervise/poll", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "proposal_id": proposal_id,
        }))

    def test_propose_queues_under_the_resolved_bottle(self) -> None:
        rec = self._register()
        status, payload = self._propose(rec)
        self.assertEqual(201, status)
        pid = payload["proposal_id"]
        assert isinstance(pid, str) and pid
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        proposals = listing["proposals"]
        assert isinstance(proposals, list)
        self.assertEqual(pid, proposals[0]["id"])
        # Queued under the orchestrator-resolved bottle id, never a caller slug.
        self.assertEqual(rec.bottle_id, proposals[0]["bottle_slug"])

    def test_propose_unattributed_is_403(self) -> None:
        status, payload = dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": "10.9.9.9", "identity_token": "wrong",
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": "x\n", "justification": "j",
        }))
        self.assertEqual(403, status)
        self.assertIn("unattributed", str(payload["error"]))

    def test_propose_rejects_unknown_tool(self) -> None:
        rec = self._register()
        status, _ = dispatch(self.orch, "POST", "/supervise/propose", _body({
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": "not-a-tool", "proposed_file": "x\n", "justification": "j",
        }))
        self.assertEqual(400, status)

    def test_poll_pending_then_decided_then_idempotent(self) -> None:
        rec = self._register()
        _, proposed = self._propose(rec)
        pid = proposed["proposal_id"]
        assert isinstance(pid, str)

        status, poll = self._poll(rec, pid)
        self.assertEqual(200, status)
        self.assertEqual("pending", poll["status"])

        # Operator decides server-side.
        dispatch(self.orch, "POST", "/supervise/respond", _body({
            "proposal_id": pid, "bottle_slug": rec.bottle_id,
            "decision": "approve", "notes": "ok",
        }))
        _, decided = self._poll(rec, pid)
        self.assertEqual("approved", decided["status"])
        self.assertEqual("ok", decided["notes"])

        # Poll doesn't archive: it's gone from the operator's pending list, but a
        # re-poll returns the same decision so a dropped connection can't lose it.
        _, listing = dispatch(self.orch, "GET", "/supervise/proposals", b"")
        self.assertEqual([], listing["proposals"])
        _, again = self._poll(rec, pid)
        self.assertEqual(decided, again)

    def test_poll_cannot_read_another_bottles_proposal(self) -> None:
        rec_a = self._register("10.0.0.1", "a")
        rec_b = self._register("10.0.0.2", "b")
        _, proposed = self._propose(rec_a)
        pid = proposed["proposal_id"]
        assert isinstance(pid, str)
        # b polls a's proposal id: scoped to b's own queue → never a's response.
        status, poll = self._poll(rec_b, pid)
        self.assertEqual(200, status)
        self.assertEqual("unknown", poll["status"])

    # --- request validation (400) + fail-closed (403) ----------------------

    def test_propose_invalid_json_is_400(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/supervise/propose", b"{not json")
        self.assertEqual(400, status)

    def test_propose_missing_fields_are_400(self) -> None:
        rec = self._register()
        base = {
            "source_ip": rec.source_ip, "identity_token": rec.identity_token,
            "tool": TOOL_EGRESS_ALLOW, "proposed_file": "routes:\n", "justification": "j",
        }
        for drop in ("source_ip", "proposed_file", "justification"):
            body = {k: v for k, v in base.items() if k != drop}
            status, _ = dispatch(self.orch, "POST", "/supervise/propose", _body(body))
            self.assertEqual(400, status, drop)

    def test_poll_invalid_json_is_400(self) -> None:
        status, _ = dispatch(self.orch, "POST", "/supervise/poll", b"{not json")
        self.assertEqual(400, status)

    def test_poll_missing_fields_are_400(self) -> None:
        rec = self._register()
        for body in ({"identity_token": rec.identity_token, "proposal_id": "p"},
                     {"source_ip": rec.source_ip, "identity_token": rec.identity_token}):
            status, _ = dispatch(self.orch, "POST", "/supervise/poll", _body(body))
            self.assertEqual(400, status)

    def test_poll_unattributed_is_403(self) -> None:
        status, payload = dispatch(self.orch, "POST", "/supervise/poll", _body({
            "source_ip": "10.9.9.9", "identity_token": "wrong", "proposal_id": "p",
        }))
        self.assertEqual(403, status)
        self.assertIn("unattributed", str(payload["error"]))


class _Live:
    """A host controller's live set, or the reason it has none."""

    def __init__(self, ips: list[str] | None = None, error: str = "") -> None:
        self.ips = ips or []
        self.error = error
        self.calls = 0

    def live_source_ips(self) -> list[str]:
        self.calls += 1
        if self.error:
            raise LiveBottlesUnknown(self.error)
        return list(self.ips)


class TestReconcileRoute(unittest.TestCase):
    """`POST /reconcile` — the orchestrator asks the host controller which
    bottles are actually up, since it can't see the backend from inside the
    infra container. Without an answer it reaps nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _old(self, source_ip: str) -> str:
        rec = self.orch.registry.register(source_ip)
        with closing(sqlite3.connect(self.orch.registry.db_path)) as conn:
            conn.execute(
                "UPDATE orchestrator_bottles SET created_at = 0.0 WHERE bottle_id = ?",
                (rec.bottle_id,))
            conn.commit()
        return rec.bottle_id

    def _reconcile(self, live: _Live | None, body: object | None = None
                   ) -> tuple[int, dict[str, object]]:
        return dispatch(self.orch, "POST", "/reconcile", _body({} if body is None else body),
                        hostctl=live)

    def test_reaps_absent_and_reports_ids(self) -> None:
        dead = self._old("10.0.0.1")
        alive = self._old("10.0.0.2")
        status, payload = self._reconcile(_Live(["10.0.0.2"]))
        self.assertEqual(200, status)
        self.assertEqual([dead], payload["reaped"])
        self.assertIsNone(self.orch.registry.get(dead))
        self.assertIsNotNone(self.orch.registry.get(alive))

    def test_without_a_host_controller_nothing_is_reaped(self) -> None:
        dead = self._old("10.0.0.1")
        status, payload = self._reconcile(None)
        self.assertEqual(503, status)
        self.assertIn("no host controller", str(payload["error"]))
        self.assertIsNotNone(self.orch.registry.get(dead))

    def test_an_unknown_live_set_reaps_nothing(self) -> None:
        dead = self._old("10.0.0.1")
        status, payload = self._reconcile(_Live(error="host controller unreachable"))
        self.assertEqual(503, status)
        self.assertIn("unreachable", str(payload["error"]))
        self.assertIsNotNone(self.orch.registry.get(dead))

    def test_live_source_ips_in_the_body_are_ignored(self) -> None:
        """The caller no longer supplies the live set; an old caller's is dropped."""
        dead = self._old("10.0.0.1")
        status, payload = self._reconcile(_Live([]), {"live_source_ips": ["10.0.0.1"]})
        self.assertEqual((200, [dead]), (status, payload["reaped"]))

    def test_grace_seconds_is_honoured(self) -> None:
        """A grace window wide enough to cover the row protects it."""
        self.orch.registry.register("10.0.0.3")
        status, payload = self._reconcile(_Live([]), {"grace_seconds": 3600})
        self.assertEqual(200, status)
        self.assertEqual([], payload["reaped"])

    def test_invalid_grace_seconds_is_rejected(self) -> None:
        for value in (True, "30", -1, float("inf"), float("nan")):
            with self.subTest(value=value):
                live = _Live([])
                status, payload = self._reconcile(live, {"grace_seconds": value})
                self.assertEqual(400, status)
                self.assertIn("grace_seconds", str(payload["error"]))
                self.assertEqual(0, live.calls)


class TestReconcileTimer(unittest.TestCase):
    """The orchestrator reconciles on a timer when a host controller brought
    it up, so orphans are reaped without a CLI running."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.orch = _orchestrator(Path(self._tmp.name) / "r.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_rounds(self, live: _Live, rounds: int) -> None:
        async def run() -> None:
            task = asyncio.create_task(
                orchestrator_api.reconcile_periodically(self.orch, live, 0.01))
            while live.calls < rounds:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(asyncio.wait_for(run(), timeout=5))

    def test_reaps_past_the_timer_grace(self) -> None:
        rec = self.orch.registry.register("10.0.0.1")
        with closing(sqlite3.connect(self.orch.registry.db_path)) as conn:
            conn.execute("UPDATE orchestrator_bottles SET created_at = 0.0")
            conn.commit()
        self._run_rounds(_Live([]), 2)
        self.assertIsNone(self.orch.registry.get(rec.bottle_id))

    def test_spares_a_bottle_inside_the_timer_grace(self) -> None:
        """Docker and firecracker register a bottle before its container or
        VM exists; the timer's wide grace leaves it alone meanwhile."""
        rec = self.orch.registry.register("10.0.0.1")
        self._run_rounds(_Live([]), 2)
        self.assertIsNotNone(self.orch.registry.get(rec.bottle_id))

    def test_keeps_going_after_a_failed_round(self) -> None:
        live = _Live(error="host controller unreachable")
        self._run_rounds(live, 3)
        self.assertGreaterEqual(live.calls, 3)

    def test_runs_only_with_a_host_controller_and_stops_with_the_app(self) -> None:
        with patch.object(orchestrator_api, "reconcile_periodically") as periodic, \
                TestClient(create_app(self.orch, signing_key="k")):
            pass
        periodic.assert_not_called()
        live = _Live([])
        with TestClient(create_app(self.orch, signing_key="k",
                                   hostctl=live,  # type: ignore[arg-type]
                                   reconcile_interval=0.01)):
            deadline = time.monotonic() + 5
            while live.calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertGreater(live.calls, 0)
        calls = live.calls
        time.sleep(0.05)
        self.assertEqual(calls, live.calls)


if __name__ == "__main__":
    unittest.main()


