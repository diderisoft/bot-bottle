"""Unit tests for framework-neutral outbound DLP request stages."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.outbound_pipeline import (
    MutableHeaders,
    redact_request,
    scan_request,
)
from bot_bottle.gateway.egress.types import Route

_TOKEN = "sk-" + "a" * 48
_TOKEN2 = "sk-" + "b" * 48


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
        request = _Request(body=f"token={_TOKEN}")

        result = scan_request(
            request,
            Route(host="api.example.com"),
            {},
            safe_tokens={_TOKEN},
        )

        self.assertIsNone(result)

    def test_secret_in_path_segment_is_detected(self) -> None:
        request = _Request(path=f"/v1/messages/{_TOKEN}")

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNotNone(result)

    def test_secret_in_query_string_is_detected(self) -> None:
        request = _Request(path=f"/v1/messages?key={_TOKEN}")

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNotNone(result)

    def test_secret_in_header_is_detected(self) -> None:
        request = _Request(headers={"X-Custom-Key": _TOKEN})

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNotNone(result)

    def test_secrets_on_all_surfaces_simultaneously(self) -> None:
        request = _Request(
            path=f"/v1/messages?key={_TOKEN}",
            headers={"X-Custom-Key": _TOKEN2},
            body=f"extra={_TOKEN}",
        )

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNotNone(result)

    def test_non_inspected_route_passes_through(self) -> None:
        request = _Request(body=f"token={_TOKEN}")

        result = scan_request(request, Route(host="api.example.com", inspect=False), {})

        self.assertIsNone(result)

    def test_empty_body_and_clean_request_is_clean(self) -> None:
        request = _Request(body="")

        result = scan_request(request, Route(host="api.example.com"), {})

        self.assertIsNone(result)

    def test_env_provisioned_secret_is_blocked(self) -> None:
        secret = "hex" + "deadbeef" * 8
        request = _Request(body=f"payload={secret}")
        env = {"EGRESS_TOKEN_MY_KEY": secret}

        result = scan_request(request, Route(host="api.example.com"), env)

        self.assertIsNotNone(result)


class TestOutboundRedaction(unittest.TestCase):
    def test_redacts_body_header_and_path_but_preserves_host(self) -> None:
        request = _Request(
            path=f"/v1/messages?key={_TOKEN}",
            headers={"Host": "api.example.com", "X-Token": _TOKEN + "\r\nInjected: yes"},
            body=f"token={_TOKEN}",
        )

        clean = redact_request(request, Route(host="api.example.com"), {})

        self.assertTrue(clean)
        self.assertNotIn(_TOKEN, request.path)
        self.assertNotIn(_TOKEN, request.headers["X-Token"])
        self.assertNotIn("\r", request.headers["X-Token"])
        self.assertNotIn(_TOKEN, request.text)
        self.assertEqual("api.example.com", request.headers["Host"])

    def test_redact_multiple_tokens_from_body(self) -> None:
        request = _Request(body=f"a={_TOKEN} b={_TOKEN2}")

        clean = redact_request(request, Route(host="api.example.com"), {})

        self.assertTrue(clean)
        self.assertNotIn(_TOKEN, request.text)
        self.assertNotIn(_TOKEN2, request.text)

    def test_redact_returns_true_for_non_inspected_route(self) -> None:
        request = _Request(body=f"token={_TOKEN}")

        clean = redact_request(request, Route(host="api.example.com", inspect=False), {})

        self.assertTrue(clean)

    def test_redact_cleans_secret_so_subsequent_scan_passes(self) -> None:
        request = _Request(body=f"token={_TOKEN}")
        route = Route(host="api.example.com")

        clean = redact_request(request, route, {})

        self.assertTrue(clean)
        self.assertIsNone(scan_request(request, route, {}))


if __name__ == "__main__":
    unittest.main()
