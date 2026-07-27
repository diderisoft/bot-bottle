"""Unit tests for framework-neutral outbound DLP request stages."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.outbound_pipeline import (
    MutableHeaders,
    redact_request,
    scan_request,
)
from bot_bottle.gateway.egress.types import Route


class _Headers(dict[str, str]):
    pass


class _Request:
    def __init__(
        self,
        *,
        host: str = "api.example.com",
        path: str = "/v1/messages",
        headers: dict[str, str] | None = None,
        body: str = "",
    ) -> None:
        self.pretty_host = host
        self.path = path
        self.headers: MutableHeaders = _Headers(headers or {})
        self.text = body

    def get_text(self, strict: bool = False) -> str | None:
        del strict
        return self.text


class TestOutboundScan(unittest.TestCase):
    def test_detects_secret_in_body(self) -> None:
        request = _Request(body="token=sk-" + "a" * 48)

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNotNone(result)
        self.assertEqual("block", result.severity if result else None)

    def test_safe_token_is_ignored(self) -> None:
        token = "sk-" + "a" * 48
        request = _Request(body=f"token={token}")

        result = scan_request(
            request,
            Route(host="api.example.com"),
            {},
            safe_tokens={token},
        )

        self.assertIsNone(result)


class TestOutboundRedaction(unittest.TestCase):
    def test_redacts_body_header_and_path_but_preserves_host(self) -> None:
        token = "sk-" + "a" * 48
        request = _Request(
            path=f"/v1/messages?key={token}",
            headers={"Host": "api.example.com", "X-Token": token + "\r\nInjected: yes"},
            body=f"token={token}",
        )

        clean = redact_request(request, Route(host="api.example.com"), {})

        self.assertTrue(clean)
        self.assertNotIn(token, request.path)
        self.assertNotIn(token, request.headers["X-Token"])
        self.assertNotIn("\r", request.headers["X-Token"])
        self.assertNotIn(token, request.text)
        self.assertEqual("api.example.com", request.headers["Host"])


if __name__ == "__main__":
    unittest.main()
