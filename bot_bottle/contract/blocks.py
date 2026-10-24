"""Why a bottle blocked a request: the closed set of `reason_code`s a gateway
block event carries in its payload, with the letter and label the session
status bar shows for each (PRD 0099).
"""

from __future__ import annotations

# Payload key on a blocked audit event.
REASON_CODE_KEY = "reason_code"

ALLOWLIST = "allowlist"
ROUTE = "route"
SECRET = "secret"
INBOUND = "inbound"
REQUEST = "request"
DENIED = "denied"
PUSH = "push"
CONFIG = "config"
OTHER = "other"  # a block event without a code

# code -> (status-bar letter, label)
REASONS: dict[str, tuple[str, str]] = {
    ALLOWLIST: ("A", "not on allowlist"),
    ROUTE: ("R", "outside route"),
    SECRET: ("S", "secret in request"),
    INBOUND: ("I", "unsafe response"),
    REQUEST: ("M", "malformed request"),
    DENIED: ("D", "you denied it"),
    PUSH: ("P", "push rejected"),
    CONFIG: ("C", "bottle config"),
    OTHER: ("?", "blocked"),
}


def reason_code(raw: object) -> str:
    """`raw` if it's a known code, else `other`."""
    return raw if isinstance(raw, str) and raw in REASONS else OTHER


__all__ = ["ALLOWLIST", "CONFIG", "DENIED", "INBOUND", "OTHER", "PUSH", "REASONS",
           "REASON_CODE_KEY", "REQUEST", "ROUTE", "SECRET", "reason_code"]
