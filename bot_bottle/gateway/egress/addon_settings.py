"""The egress proxy's env-tunable limits, parsed at startup."""

from __future__ import annotations

import os
import sys

# Seconds the egress proxy holds a token-blocked request open waiting for the
# operator's supervisor decision, overridable via env.
DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS = 300.0

# Maximum bytes of a response body passed to the DLP inbound scan. mitmproxy
# buffers the full response before the hook fires; capping at scan time limits
# the additional memory amplification from decoded text and regex match strings.
# A cap is a security trade-off (content above the threshold is not scanned),
# but without it a single large download OOM-kills the shared egress process
#. Override with EGRESS_INBOUND_SCAN_LIMIT_BYTES; set to 0 to
# disable the cap.
DEFAULT_INBOUND_SCAN_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB


def _inbound_scan_limit_from_env(env: "os._Environ[str]") -> int:
    """Read EGRESS_INBOUND_SCAN_LIMIT_BYTES; fall back to the default on an
    unset or invalid value. Returns 0 to disable the cap."""
    raw = env.get("EGRESS_INBOUND_SCAN_LIMIT_BYTES", "").strip()
    if not raw:
        return DEFAULT_INBOUND_SCAN_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        sys.stderr.write(
            "egress: invalid EGRESS_INBOUND_SCAN_LIMIT_BYTES="
            f"{raw!r}; using default {DEFAULT_INBOUND_SCAN_LIMIT_BYTES}\n"
        )
        return DEFAULT_INBOUND_SCAN_LIMIT_BYTES
    return value


def _token_allow_timeout_from_env(env: "os._Environ[str]") -> float:
    """Read EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS; fall back to the default on an
    unset or invalid value (a bad value should not wedge egress at boot)."""
    raw = env.get("EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        sys.stderr.write(
            "egress: invalid EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS="
            f"{raw!r}; using default {DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS:g}s\n"
        )
        return DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS
    return value


__all__ = [
    "DEFAULT_INBOUND_SCAN_LIMIT_BYTES",
    "DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS",
    "_inbound_scan_limit_from_env",
    "_token_allow_timeout_from_env",
]
