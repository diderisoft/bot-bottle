"""Shared egress policy value objects.

Kept dependency-free so the schema parser, matcher, DLP scanner, and addon
adapter can use the same immutable public shapes without importing each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


PATH_MATCH_TYPES = ("exact", "prefix", "regex")
HEADER_MATCH_TYPES = ("exact", "regex")
VALID_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
    "CONNECT",
})

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
    git_fetch: bool = False
    outbound_detectors: tuple[str, ...] | None = None
    inbound_detectors: tuple[str, ...] | None = None
    outbound_on_match: str = ""
    preserve_auth: bool = False
    inspect: bool = True


@dataclass(frozen=True)
class Config:
    routes: tuple[Route, ...]
    log: int = LOG_OFF
    deny_reason: str = ""
    # The bottle's `audit.delivery`: `required` holds an allowed request
    # until its audit event is written, and refuses it if it can't be.
    audit_delivery: str = "best_effort"
    # The bottle's `audit.firehose`: `metadata` keeps its request records to
    # method, host, port, decision, status, size and timing.
    firehose: str = "full"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str = ""
    inject_authorization: str | None = None


@dataclass(frozen=True)
class ScanResult:
    severity: str
    reason: str
    location: str = ""
    context: str = ""
    matched: str = ""
    # A hit on the bottle's exfil canary: always a hard block, never
    # supervised, safelisted or redacted-and-forwarded.
    canary: bool = False
