"""Shared egress policy value objects, dependency-free."""

from __future__ import annotations

import re
from dataclasses import dataclass


PATH_MATCH_TYPES = ("exact", "prefix", "regex")
HEADER_MATCH_TYPES = ("exact", "regex")
VALID_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
    "CONNECT",
})

_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
# Headers a route may not inject its token into: framing, routing and the
# cookie jar's.
RESERVED_AUTH_HEADERS = frozenset({
    "host", "cookie", "proxy-authorization", "content-length",
    "transfer-encoding", "connection", "te", "trailer", "upgrade",
    "keep-alive",
})


def auth_header_error(name: str) -> str:
    """Why `name` can't be a route's auth header, or ""."""
    if not _HEADER_NAME.match(name):
        return f"auth header {name!r} is not a valid HTTP header name"
    if name.lower() in RESERVED_AUTH_HEADERS:
        return f"auth header {name!r} is reserved"
    return ""


LOG_OFF = 0
LOG_BLOCKS = 1
LOG_FULL = 2


@dataclass(frozen=True)
class PathMatch:
    type: str
    value: str
    compiled: re.Pattern[str] | None = None


@dataclass(frozen=True)
class HeaderMatch:
    name: str
    value: str
    type: str = "exact"
    compiled: re.Pattern[str] | None = None


@dataclass(frozen=True)
class MatchEntry:
    paths: tuple[PathMatch, ...] = ()
    methods: tuple[str, ...] = ()
    headers: tuple[HeaderMatch, ...] = ()


@dataclass(frozen=True)
class Route:
    host: str
    matches: tuple[MatchEntry, ...] = ()
    auth_scheme: str = ""
    token_env: str = ""
    # The header the injected token goes in; "" is `Authorization`. A named
    # header with no `auth_scheme` carries the raw token (e.g. `x-api-key`).
    auth_header: str = ""
    git_fetch: bool = False
    outbound_detectors: tuple[str, ...] | None = None
    inbound_detectors: tuple[str, ...] | None = None
    outbound_on_match: str = ""
    preserve_auth: bool = False
    inspect: bool = True

    @property
    def injects_auth(self) -> bool:
        """The gateway injects this route's token upstream."""
        return bool(self.token_env and (self.auth_scheme or self.auth_header))

    @property
    def injected_header(self) -> str:
        """The lowercased header the injected token goes in."""
        return (self.auth_header or "authorization").lower()

    def auth_value(self, token: str) -> str:
        """The injected header's value: `<scheme> <token>`, or the raw token
        when the route names a header with no scheme."""
        return f"{self.auth_scheme} {token}" if self.auth_scheme else token


@dataclass(frozen=True)
class Config:
    routes: tuple[Route, ...]
    log: int = LOG_OFF
    deny_reason: str = ""
    # `audit.delivery`: `required` holds an allowed request until its event is
    # written, refusing it otherwise.
    audit_delivery: str = "best_effort"
    # `audit.firehose`: `metadata` limits records to method, host, port,
    # decision, status, size and timing.
    firehose: str = "full"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""
    inject_authorization: str | None = None
    # The header `inject_authorization` goes in (lowercased).
    inject_header: str = "authorization"
    code: str = ""  # a block's reason code (contract.blocks)


@dataclass(frozen=True)
class ScanResult:
    severity: str
    reason: str
    location: str = ""
    context: str = ""
    matched: str = ""
    # A canary hit: always a hard block.
    canary: bool = False
