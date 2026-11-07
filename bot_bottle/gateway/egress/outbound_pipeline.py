"""Outbound DLP request scanning and redaction for the egress pipeline."""

from __future__ import annotations

from typing import AbstractSet, ItemsView, Mapping, Protocol

from .dlp import (
    build_outbound_scan_text,
    outbound_scan_headers,
    scan_outbound,
)
from .dlp_detectors import redact_tokens, strip_crlf
from .types import Route, ScanResult


class MutableHeaders(Protocol):
    def items(self) -> ItemsView[str, str]: ...
    def __getitem__(self, name: str, /) -> str: ...
    def __setitem__(self, name: str, value: str, /) -> None: ...


class MutableRequest(Protocol):
    pretty_host: str
    path: str
    headers: MutableHeaders
    text: str

    def get_text(self, strict: bool = False) -> str | None: ...


def scan_request(
    request: MutableRequest,
    route: Route,
    env: Mapping[str, str],
    *,
    safe_tokens: AbstractSet[str] | None = None,
) -> ScanResult | None:
    """Scan every mutable outbound request surface, in canonical order."""
    request_path, _, query = request.path.partition("?")
    headers = outbound_scan_headers(route, dict(request.headers.items()))
    body = request.get_text(strict=False) or ""
    scan_text = build_outbound_scan_text(
        request.pretty_host, request_path, query, headers, body,
    )
    # Bodies can't alter HTTP framing, so CRLF checks cover the request line
    # and headers only.
    crlf_text = build_outbound_scan_text(
        request.pretty_host, request_path, query, headers, "",
    )
    return scan_outbound(
        route,
        scan_text,
        env,
        safe_tokens=safe_tokens,
        crlf_text=crlf_text,
    )


def redact_request(
    request: MutableRequest,
    route: Route,
    env: Mapping[str, str],
) -> bool:
    """Redact the mutable request surfaces; True if the result is clean."""
    body = request.get_text(strict=False)
    if body:
        redacted_body = redact_tokens(body, env=env)
        if redacted_body != body:
            request.text = redacted_body
    for name, value in list(request.headers.items()):
        if name.lower() == "host":
            continue
        redacted = strip_crlf(redact_tokens(value, env=env))
        if redacted != value:
            request.headers[name] = redacted
    redacted_path = strip_crlf(redact_tokens(request.path, env=env))
    if redacted_path != request.path:
        request.path = redacted_path
    result = scan_request(request, route, env)
    return result is None or result.severity != "block"


__all__ = ["MutableRequest", "redact_request", "scan_request"]
