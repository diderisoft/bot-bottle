"""Unit: EgressAddon request/response decision flow (issue #286).

`egress_addon.py` is the sidecar-only mitmproxy adapter that wires the
host-importable decision logic in `egress_addon_core` into mitmproxy's
request/response hooks. The core logic is exercised directly by
`test_egress_addon_core.py`; the redaction logging by
`test_egress_addon_log_redaction.py`. This file covers the adapter glue
itself — `request()`, `response()`, `websocket_message()`, introspection,
auth injection, git push/fetch blocking and the outbound-DLP policy
branches — so `bot_bottle/egress_addon.py` no longer has to be omitted
from coverage.

mitmproxy is not installed on the host, so we pre-populate `sys.modules`
with the minimum stubs needed to import the adapter (a `mitmproxy.http`
module exposing a `Response` with `.make`, plus the flat
`egress_addon_core` name the sidecar uses)."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Stub flow objects (mirror the slice of mitmproxy's API the adapter uses)
# ---------------------------------------------------------------------------


class _Headers:
    """Case-insensitive header map covering the subset of mitmproxy's
    Headers API the adapter touches: items/get/pop/__setitem__/dict()."""

    def __init__(self, d: dict[str, str] | None = None) -> None:
        self._d: dict[str, str] = dict(d or {})

    def _find(self, key: str) -> str | None:
        return next((k for k in self._d if k.lower() == key.lower()), None)

    def items(self) -> list[tuple[str, str]]:
        return list(self._d.items())

    def keys(self) -> list[str]:
        return list(self._d.keys())

    def __iter__(self) -> Any:
        return iter(self._d)

    def __getitem__(self, key: str) -> str:
        k = self._find(key)
        if k is None:
            raise KeyError(key)
        return self._d[k]

    def __setitem__(self, key: str, value: str) -> None:
        self._d[self._find(key) or key] = value

    def __contains__(self, key: str) -> bool:
        return self._find(key) is not None

    def get(self, key: str, default: str | None = None) -> str | None:
        k = self._find(key)
        return self._d[k] if k is not None else default

    def pop(self, key: str, default: str | None = None) -> str | None:
        k = self._find(key)
        return self._d.pop(k) if k is not None else default


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content: bytes | str = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = _Headers(headers)
        self._body = (
            content if isinstance(content, str)
            else content.decode("utf-8", "replace")
        )

    def get_text(self, *, strict: bool = True) -> str:
        del strict
        return self._body

    @classmethod
    def make(
        cls,
        status_code: int = 200,
        content: bytes | str = b"",
        headers: dict[str, str] | None = None,
    ) -> "_Response":
        return cls(status_code, headers, content)


class _Request:
    def __init__(
        self,
        host: str = "api.example.com",
        method: str = "GET",
        path: str = "/v1/messages",
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.pretty_host = host
        self.method = method
        self.path = path
        self.headers = _Headers(headers)
        self._body = body

    def get_text(self, *, strict: bool = True) -> str:
        del strict
        return self._body

    @property
    def text(self) -> str:
        return self._body

    @text.setter
    def text(self, value: str) -> None:
        self._body = value


class _Flow:
    def __init__(
        self,
        request: _Request | None = None,
        response: _Response | None = None,
    ) -> None:
        self.request = request or _Request()
        self.response = response
        self.websocket: Any = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True


class _Message:
    def __init__(self, content: bytes, from_client: bool) -> None:
        self.content = content
        self.from_client = from_client


class _WebSocketData:
    def __init__(self, messages: list[_Message]) -> None:
        self.messages = messages


# ---------------------------------------------------------------------------
# Sidecar-import shims — must run before importing egress_addon
# ---------------------------------------------------------------------------


def _ensure_shims() -> None:
    mm = sys.modules.get("mitmproxy")
    if mm is None:
        mm = types.ModuleType("mitmproxy")
        sys.modules["mitmproxy"] = mm
    mh = sys.modules.get("mitmproxy.http")
    if mh is None:
        mh = types.ModuleType("mitmproxy.http")
        sys.modules["mitmproxy.http"] = mh
        setattr(mm, "http", mh)
    # Other egress_addon tests may have registered an empty mitmproxy.http;
    # make sure the Response/HTTPFlow attrs the request flow needs exist.
    if not hasattr(mh, "Response"):
        setattr(mh, "Response", _Response)
    if not hasattr(mh, "HTTPFlow"):
        setattr(mh, "HTTPFlow", object)
    if "egress_addon_core" not in sys.modules:
        import bot_bottle.egress_addon_core as _core
        sys.modules["egress_addon_core"] = _core


_ensure_shims()

import bot_bottle.egress_addon as _ea_mod  # noqa: E402  (after shims)
from bot_bottle.egress_addon import EgressAddon  # noqa: E402  (after shims)
from bot_bottle.egress_addon_core import (  # noqa: E402
    Config,
    LOG_BLOCKS,
    Route,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_OPENAI_KEY = "sk-" + "A" * 48


def _addon(config: Config) -> EgressAddon:
    """Bare EgressAddon with a supplied config and no supervise wiring."""
    a: EgressAddon = EgressAddon.__new__(EgressAddon)
    a.config = config
    a.safe_tokens = set()
    a._supervise_queue_dir = ""
    a._supervise_slug = ""
    a._token_allow_timeout = 300.0
    a.routes_path = "/nonexistent/routes.yaml"
    return a


def _run_request(addon: EgressAddon, flow: _Flow) -> None:
    asyncio.run(addon.request(flow))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Introspection endpoint
# ---------------------------------------------------------------------------


class TestIntrospection(unittest.TestCase):
    def test_allowlist_endpoint_lists_routes(self) -> None:
        addon = _addon(Config(routes=(Route(host="api.example.com"),)))
        flow = _Flow(_Request(host="_egress.local", path="/allowlist"))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(200, flow.response.status_code)
        payload = json.loads(flow.response.get_text())
        self.assertEqual(["api.example.com"], [r["host"] for r in payload["routes"]])

    def test_unknown_endpoint_404(self) -> None:
        addon = _addon(Config(routes=()))
        flow = _Flow(_Request(host="_egress.local", path="/nope"))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(404, flow.response.status_code)


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------


class TestAllowlist(unittest.TestCase):
    def test_unlisted_host_blocked_403(self) -> None:
        addon = _addon(Config(routes=(Route(host="allowed.example.com"),)))
        flow = _Flow(_Request(host="evil.example.com"))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)
        self.assertIn("allowlist", flow.response.get_text())

    def test_listed_host_forwarded_no_response_written(self) -> None:
        addon = _addon(Config(routes=(Route(host="api.example.com"),)))
        flow = _Flow(_Request(host="api.example.com"))
        _run_request(addon, flow)
        # forward == adapter leaves flow.response untouched for the upstream
        self.assertIsNone(flow.response)


# ---------------------------------------------------------------------------
# Authorization stripping + injection
# ---------------------------------------------------------------------------


class TestAuthInjection(unittest.TestCase):
    def test_agent_authorization_stripped_and_real_token_injected(self) -> None:
        route = Route(host="api.example.com", auth_scheme="Bearer", token_env="EGRESS_TOKEN_0")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com", headers={"authorization": "Bearer agent-faked"}))
        with patch.dict("os.environ", {"EGRESS_TOKEN_0": "real-sidecar-token"}):
            _run_request(addon, flow)
        self.assertEqual("Bearer real-sidecar-token", flow.request.headers.get("authorization"))
        self.assertIsNone(flow.response)

    def test_auth_route_with_unset_env_blocks(self) -> None:
        route = Route(
            host="api.example.com", auth_scheme="Bearer", token_env="EGRESS_TOKEN_MISSING",
        )
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com"))
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("EGRESS_TOKEN_MISSING", None)
            _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)


# ---------------------------------------------------------------------------
# git push / fetch over HTTPS
# ---------------------------------------------------------------------------


class TestGitOverHttps(unittest.TestCase):
    def test_git_push_blocked(self) -> None:
        addon = _addon(Config(routes=(Route(host="git.example.com"),)))
        flow = _Flow(_Request(
            host="git.example.com",
            method="POST",
            path="/repo.git/git-receive-pack",
        ))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)
        self.assertIn("git push over HTTPS", flow.response.get_text())

    def test_git_fetch_blocked_on_non_fetch_route(self) -> None:
        addon = _addon(Config(routes=(Route(host="git.example.com"),)))
        flow = _Flow(_Request(
            host="git.example.com",
            path="/repo.git/info/refs",
        ))
        flow.request.path = "/repo.git/info/refs?service=git-upload-pack"
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)

    def test_git_fetch_allowed_on_fetch_route(self) -> None:
        addon = _addon(Config(routes=(Route(host="git.example.com", git_fetch=True),)))
        flow = _Flow(_Request(
            host="git.example.com",
            path="/repo.git/info/refs?service=git-upload-pack",
        ))
        _run_request(addon, flow)
        self.assertIsNone(flow.response)


# ---------------------------------------------------------------------------
# Outbound DLP policy branches
# ---------------------------------------------------------------------------


class TestOutboundDlpPolicy(unittest.TestCase):
    def test_block_policy_hard_403(self) -> None:
        route = Route(host="api.example.com", outbound_on_match="block")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"key={_OPENAI_KEY}"))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)
        self.assertIn("DLP", flow.response.get_text())

    def test_redact_policy_scrubs_and_forwards(self) -> None:
        route = Route(host="api.example.com", outbound_on_match="redact")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"key={_OPENAI_KEY}"))
        _run_request(addon, flow)
        self.assertIsNone(flow.response)  # forwarded
        self.assertNotIn(_OPENAI_KEY, flow.request.get_text())

    def test_supervise_default_without_wiring_blocks(self) -> None:
        # outbound_on_match unset -> supervise default; no supervise queue wired
        # -> fail closed with a hard 403.
        route = Route(host="api.example.com")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"key={_OPENAI_KEY}"))
        _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)


# ---------------------------------------------------------------------------
# Outbound DLP supervise branch (operator approval round-trip)
# ---------------------------------------------------------------------------


def _fake_sv(response_status: str | None) -> types.SimpleNamespace:
    """Stand-in for the `supervise` module the adapter queues proposals to.

    `response_status` of None models a timeout (read_response never returns a
    decision); a status string models the operator's eventual answer."""
    def _new_proposal(**_kw: Any) -> Any:
        return types.SimpleNamespace(id="prop-1")

    def _sha256_hex(_payload: Any) -> str:
        return "hash"

    def _noop(_a: Any, _b: Any) -> None:
        return None

    def _read_response(_qd: Any, _pid: Any) -> Any:
        if response_status is None:
            raise OSError("not written yet")  # forces poll -> timeout
        return types.SimpleNamespace(status=response_status)

    ns = types.SimpleNamespace()
    ns.STATUS_APPROVED = "approved"
    ns.STATUS_MODIFIED = "modified"
    ns.TOOL_EGRESS_TOKEN_ALLOW = "egress_token_allow"
    ns.Proposal = types.SimpleNamespace(new=_new_proposal)
    ns.sha256_hex = _sha256_hex
    ns.write_proposal = _noop
    ns.archive_proposal = _noop
    ns.read_response = _read_response
    return ns


class TestSuperviseBranch(unittest.TestCase):
    def _supervised_addon(self) -> EgressAddon:
        addon = _addon(Config(routes=(Route(host="api.example.com"),)))
        addon._supervise_queue_dir = "/tmp/egress-queue"
        addon._supervise_slug = "test-bottle"
        addon._token_allow_timeout = 0.05
        return addon

    def test_operator_approval_allows_token_and_forwards(self) -> None:
        addon = self._supervised_addon()
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"k={_OPENAI_KEY}"))
        with patch.object(_ea_mod, "_sv", _fake_sv("approved")):
            _run_request(addon, flow)
        self.assertIsNone(flow.response)  # forwarded after approval
        self.assertIn(_OPENAI_KEY, addon.safe_tokens)

    def test_operator_rejection_blocks(self) -> None:
        addon = self._supervised_addon()
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"k={_OPENAI_KEY}"))
        with patch.object(_ea_mod, "_sv", _fake_sv("rejected")):
            _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)
        self.assertIn("rejected", flow.response.get_text())

    def test_supervise_timeout_blocks(self) -> None:
        addon = self._supervised_addon()
        flow = _Flow(_Request(host="api.example.com", method="POST", body=f"k={_OPENAI_KEY}"))
        with patch.object(_ea_mod, "_sv", _fake_sv(None)):
            _run_request(addon, flow)
        assert flow.response is not None
        self.assertEqual(403, flow.response.status_code)
        self.assertIn("timed out", flow.response.get_text())


# ---------------------------------------------------------------------------
# Inbound DLP on responses
# ---------------------------------------------------------------------------


class TestInboundResponseScan(unittest.TestCase):
    def test_clean_response_untouched(self) -> None:
        route = Route(host="api.example.com")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(
            _Request(host="api.example.com"),
            _Response(200, content='{"ok": true}'),
        )
        addon.response(flow)  # type: ignore[arg-type]
        assert flow.response is not None
        self.assertEqual(200, flow.response.status_code)

    def test_response_for_unlisted_host_is_noop(self) -> None:
        addon = _addon(Config(routes=()))
        flow = _Flow(_Request(host="api.example.com"), _Response(200, content="x"))
        addon.response(flow)  # type: ignore[arg-type]
        assert flow.response is not None
        self.assertEqual(200, flow.response.status_code)


# ---------------------------------------------------------------------------
# WebSocket frame scanning
# ---------------------------------------------------------------------------


class TestWebSocket(unittest.TestCase):
    def test_outbound_frame_with_token_kills_connection(self) -> None:
        route = Route(host="api.example.com")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com"))
        flow.websocket = _WebSocketData([_Message(f"k={_OPENAI_KEY}".encode(), from_client=True)])
        addon.websocket_message(flow)  # type: ignore[arg-type]
        self.assertTrue(flow.killed)

    def test_clean_outbound_frame_passes(self) -> None:
        route = Route(host="api.example.com")
        addon = _addon(Config(routes=(route,)))
        flow = _Flow(_Request(host="api.example.com"))
        flow.websocket = _WebSocketData([_Message(b"hello world", from_client=True)])
        addon.websocket_message(flow)  # type: ignore[arg-type]
        self.assertFalse(flow.killed)

    def test_unlisted_host_websocket_is_noop(self) -> None:
        addon = _addon(Config(routes=()))
        flow = _Flow(_Request(host="api.example.com"))
        flow.websocket = _WebSocketData([_Message(f"k={_OPENAI_KEY}".encode(), from_client=True)])
        addon.websocket_message(flow)  # type: ignore[arg-type]
        self.assertFalse(flow.killed)


# ---------------------------------------------------------------------------
# _block logging + config reload via the real file path
# ---------------------------------------------------------------------------


class TestBlockLoggingAndReload(unittest.TestCase):
    def test_block_emits_json_log_when_enabled(self) -> None:
        addon = _addon(Config(routes=(Route(host="allowed.example.com"),), log=LOG_BLOCKS))
        flow = _Flow(_Request(host="evil.example.com"))
        buf = StringIO()
        with patch("sys.stderr", buf):
            _run_request(addon, flow)
        logged = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(e.get("event") == "egress_block" for e in logged))

    def test_init_loads_routes_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            routes = Path(d) / "routes.yaml"
            routes.write_text("routes:\n  - host: api.example.com\n", encoding="utf-8")
            with patch.dict("os.environ", {"EGRESS_ROUTES": str(routes)}):
                addon = EgressAddon()
            self.assertEqual(("api.example.com",), tuple(r.host for r in addon.config.routes))

    def test_init_missing_routes_file_is_empty_config(self) -> None:
        with patch.dict("os.environ", {"EGRESS_ROUTES": "/no/such/routes.yaml"}):
            buf = StringIO()
            with patch("sys.stderr", buf):
                addon = EgressAddon()
        self.assertEqual((), addon.config.routes)


if __name__ == "__main__":
    unittest.main()
