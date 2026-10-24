"""DLP scan dispatch and safe proposal rendering for egress requests."""

from __future__ import annotations

import dataclasses
import typing

from ...contract.held_secrets import CANARY_KEY, EGRESS_TOKEN_PREFIX
from .types import Route, ScanResult


def build_outbound_scan_text(host: str, path: str, query: str,
                             headers: typing.Mapping[str, str], body: str) -> str:
    parts = [host, path]
    if query:
        parts.append(query)
    parts.extend(f"{name}: {value}" for name, value in headers.items())
    if body:
        parts.append(body)
    return "\n".join(parts)


def outbound_scan_headers(route: Route, headers: typing.Mapping[str, str]) -> dict[str, str]:
    """Drop agent Authorization when the route injects gateway-owned auth."""
    skip_auth = bool(route.auth_scheme and route.token_env)
    return {name: value for name, value in headers.items()
            if not (skip_auth and name.lower() == "authorization")}


def build_inbound_scan_text(headers: typing.Mapping[str, str], body: str) -> str:
    parts = [f"{name}: {value}" for name, value in headers.items()]
    if body:
        parts.append(body)
    return "\n".join(parts)


def _enabled(configured: tuple[str, ...] | None, name: str) -> bool:
    return configured is None or name in configured


def _scan_canary(text: str, environ: typing.Mapping[str, str]) -> ScanResult | None:
    """A hit on the bottle's canary. Checked on every inspected route, never
    safelisted."""
    from .dlp_detectors import scan_known_secrets
    canary = environ.get(CANARY_KEY, "")
    if not canary:
        return None
    result = scan_known_secrets(text, location="body", env={CANARY_KEY: canary},
                                sensitive_prefixes=(CANARY_KEY,))
    return dataclasses.replace(result, canary=True) if result is not None else None


def scan_outbound(route: Route, body: str | bytes, environ: typing.Mapping[str, str], *,
                  safe_tokens: typing.AbstractSet[str] | None = None,
                  crlf_text: str | None = None) -> ScanResult | None:
    if not route.inspect:
        return None
    try:
        from dlp_detectors import (  # type: ignore[import-not-found]
            scan_crlf_injection, scan_entropy, scan_known_secrets, scan_token_patterns)
    except ImportError:  # pragma: no cover - gateway's flat module path
        from .dlp_detectors import (
            scan_crlf_injection, scan_entropy, scan_known_secrets, scan_token_patterns)
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
    else:
        text = body
    result = (scan_crlf_injection(text if crlf_text is None else crlf_text)
              or _scan_canary(text, environ))
    if result is not None:
        return result
    if _enabled(route.outbound_detectors, "token_patterns"):
        result = scan_token_patterns(text, location="body", safe_tokens=safe_tokens)
        if result is not None:
            return result
    if _enabled(route.outbound_detectors, "known_secrets"):
        extra = tuple(prefix for prefix in environ.get(
            "BOT_BOTTLE_SENSITIVE_PREFIXES", "").split(",") if prefix)
        result = scan_known_secrets(text, location="body", env=environ,
                                    sensitive_prefixes=(EGRESS_TOKEN_PREFIX,) + extra,
                                    safe_tokens=safe_tokens)
        if result is not None:
            return result
    if route.outbound_detectors is not None and "entropy" in route.outbound_detectors:
        return scan_entropy(text, location="body")
    return None


def build_token_allow_payload(host: str, method: str, path: str, result: ScanResult) -> str:
    """Redacted operator context; the raw match is left out."""
    lines = [
        "egress blocked an outbound request carrying a detected token",
        f"host: {host}", f"method: {method}", f"path: {path}",
        f"detector: {result.reason}",
    ]
    if result.context:
        lines.append(f"context: {result.context}")
    return "\n".join(lines) + "\n"


def scan_inbound(route: Route, body: str | bytes) -> ScanResult | None:
    if not route.inspect:
        return None
    try:
        from dlp_detectors import scan_naive_injection  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - gateway's flat module path
        from .dlp_detectors import scan_naive_injection
    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")
    if _enabled(route.inbound_detectors, "naive_injection_detection"):
        return scan_naive_injection(text)
    return None
