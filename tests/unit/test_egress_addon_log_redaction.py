"""Unit: LOG_FULL credential redaction in _log_request / _log_response (issue #257).

egress_addon.py is sidecar-only code that depends on mitmproxy, which is
not installed on the host. This file pre-populates sys.modules with the
minimum mocks needed so EgressAddon can be imported and tested without the
real mitmproxy package."""

from __future__ import annotations

import json
import sys
import types
import unittest
from io import StringIO
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Sidecar-import shims — must run before importing egress_addon
# ---------------------------------------------------------------------------

def _ensure_shims() -> None:
    if "mitmproxy" not in sys.modules:
        _mm = types.ModuleType("mitmproxy")
        _mh = types.ModuleType("mitmproxy.http")
        setattr(_mm, "http", _mh)
        sys.modules["mitmproxy"] = _mm
        sys.modules["mitmproxy.http"] = _mh
    if "egress_addon_core" not in sys.modules:
        import bot_bottle.egress_addon_core as _core
        sys.modules["egress_addon_core"] = _core


_ensure_shims()

from bot_bottle.egress_addon import EgressAddon  # noqa: E402  (import after shims)
from bot_bottle.egress_addon_core import Config, LOG_FULL  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _addon() -> EgressAddon:
    """Return a bare EgressAddon with LOG_FULL config and no routes file."""
    a: EgressAddon = EgressAddon.__new__(EgressAddon)
    a.config = Config(routes=(), log=LOG_FULL)
    a.safe_tokens = set()
    a._supervise_queue_dir = ""
    a._supervise_slug = ""
    a._token_allow_timeout = 300.0
    return a


class _Headers:
    def __init__(self, d: dict[str, str]) -> None:
        self._d = d

    def items(self) -> list[tuple[str, str]]:
        return list(self._d.items())


class _Request:
    def __init__(
        self,
        host: str = "api.example.com",
        method: str = "POST",
        path: str = "/v1/messages",
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.pretty_host = host
        self.method = method
        self.path = path
        self.headers = _Headers(headers or {})
        self._body = body

    def get_text(self, *, strict: bool = True) -> str:
        return self._body


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = _Headers(headers or {})
        self._body = body

    def get_text(self, *, strict: bool = True) -> str:
        return self._body


class _Flow:
    def __init__(
        self,
        request: _Request | None = None,
        response: _Response | None = None,
    ) -> None:
        self.request = request or _Request()
        self.response = response or _Response()


def _log_request(addon: EgressAddon, flow: _Flow) -> dict[str, Any]:
    buf = StringIO()
    with patch("sys.stderr", buf):
        addon._log_request(flow)  # type: ignore[arg-type]
    return json.loads(buf.getvalue())


def _log_response(addon: EgressAddon, flow: _Flow) -> dict[str, Any]:
    buf = StringIO()
    with patch("sys.stderr", buf):
        addon._log_response(flow)  # type: ignore[arg-type]
    return json.loads(buf.getvalue())


# ---------------------------------------------------------------------------
# _log_request — authorization header stripped
# ---------------------------------------------------------------------------


class TestLogRequestAuthorizationStripped(unittest.TestCase):
    def test_lowercase_authorization_excluded(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={"authorization": "Bearer sk-real-secret"}))
        entry = _log_request(addon, flow)
        self.assertNotIn("authorization", entry["headers"])

    def test_titlecase_authorization_excluded(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={"Authorization": "Bearer sk-real-secret"}))
        entry = _log_request(addon, flow)
        self.assertNotIn("Authorization", entry["headers"])
        self.assertNotIn("authorization", entry["headers"])

    def test_non_auth_headers_retained(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={
            "authorization": "Bearer sk-real-secret",
            "content-type": "application/json",
        }))
        entry = _log_request(addon, flow)
        self.assertIn("content-type", entry["headers"])
        self.assertEqual("application/json", entry["headers"]["content-type"])

    def test_no_authorization_header_logs_all_others(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={"x-request-id": "abc"}))
        entry = _log_request(addon, flow)
        self.assertEqual({"x-request-id": "abc"}, entry["headers"])


# ---------------------------------------------------------------------------
# _log_request — body redaction
# ---------------------------------------------------------------------------


_OPENAI_KEY = "sk-" + "A" * 48


class TestLogRequestBodyRedacted(unittest.TestCase):
    def test_token_pattern_in_body_scrubbed(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(body=f"key={_OPENAI_KEY}"))
        entry = _log_request(addon, flow)
        self.assertNotIn(_OPENAI_KEY, entry["body"])
        self.assertIn("********", entry["body"])

    def test_provisioned_secret_in_body_scrubbed(self) -> None:
        addon = _addon()
        secret = "provisioned-egress-secret-xyz"
        flow = _Flow(request=_Request(body=f"token={secret}"))
        with patch.dict("os.environ", {"EGRESS_TOKEN_0": secret}):
            entry = _log_request(addon, flow)
        self.assertNotIn(secret, entry["body"])
        self.assertIn("********", entry["body"])

    def test_clean_body_preserved(self) -> None:
        addon = _addon()
        payload = '{"model": "claude-3", "max_tokens": 1024}'
        flow = _Flow(request=_Request(body=payload))
        entry = _log_request(addon, flow)
        self.assertEqual(payload, entry["body"])


# ---------------------------------------------------------------------------
# _log_request — non-authorization header value redaction
# ---------------------------------------------------------------------------


class TestLogRequestHeaderValuesRedacted(unittest.TestCase):
    def test_token_in_custom_header_scrubbed(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={"x-api-key": _OPENAI_KEY}))
        entry = _log_request(addon, flow)
        self.assertNotIn(_OPENAI_KEY, entry["headers"].get("x-api-key", ""))
        self.assertIn("********", entry["headers"].get("x-api-key", ""))

    def test_clean_header_value_preserved(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(headers={"accept": "application/json"}))
        entry = _log_request(addon, flow)
        self.assertEqual("application/json", entry["headers"]["accept"])


# ---------------------------------------------------------------------------
# _log_response — body redaction
# ---------------------------------------------------------------------------


class TestLogResponseBodyRedacted(unittest.TestCase):
    def test_token_pattern_in_response_body_scrubbed(self) -> None:
        addon = _addon()
        flow = _Flow(
            request=_Request(),
            response=_Response(body=f'{{"key": "{_OPENAI_KEY}"}}'),
        )
        entry = _log_response(addon, flow)
        self.assertNotIn(_OPENAI_KEY, entry["body"])
        self.assertIn("********", entry["body"])

    def test_provisioned_secret_in_response_body_scrubbed(self) -> None:
        addon = _addon()
        secret = "provisioned-egress-secret-xyz"
        flow = _Flow(
            request=_Request(),
            response=_Response(body=f'{{"token": "{secret}"}}'),
        )
        with patch.dict("os.environ", {"EGRESS_TOKEN_0": secret}):
            entry = _log_response(addon, flow)
        self.assertNotIn(secret, entry["body"])
        self.assertIn("********", entry["body"])

    def test_clean_response_body_preserved(self) -> None:
        addon = _addon()
        flow = _Flow(request=_Request(), response=_Response(body='{"result": "ok"}'))
        entry = _log_response(addon, flow)
        self.assertEqual('{"result": "ok"}', entry["body"])


# ---------------------------------------------------------------------------
# _log_response — response header value redaction
# ---------------------------------------------------------------------------


class TestLogResponseHeaderValuesRedacted(unittest.TestCase):
    def test_token_in_response_header_scrubbed(self) -> None:
        addon = _addon()
        flow = _Flow(
            request=_Request(),
            response=_Response(headers={"set-cookie": f"token={_OPENAI_KEY}"}),
        )
        entry = _log_response(addon, flow)
        cookie_val = entry["headers"].get("set-cookie", "")
        self.assertNotIn(_OPENAI_KEY, cookie_val)
        self.assertIn("********", cookie_val)

    def test_clean_response_header_preserved(self) -> None:
        addon = _addon()
        flow = _Flow(
            request=_Request(),
            response=_Response(headers={"content-type": "application/json"}),
        )
        entry = _log_response(addon, flow)
        self.assertEqual("application/json", entry["headers"]["content-type"])


if __name__ == "__main__":
    unittest.main()
