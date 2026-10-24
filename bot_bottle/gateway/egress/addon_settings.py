"""The egress proxy's env-tunable limits."""

from __future__ import annotations

import os
import sys

# How long a token-blocked request waits for the operator.
DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS = 300.0

# Max response bytes passed to inbound DLP; larger content isn't scanned, but
# without a cap one big download can OOM the shared proxy.
# `EGRESS_INBOUND_SCAN_LIMIT_BYTES` overrides; 0 disables.
DEFAULT_INBOUND_SCAN_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB


def _inbound_scan_limit_from_env(env: "os._Environ[str]") -> int:
    """`EGRESS_INBOUND_SCAN_LIMIT_BYTES`, or the default if unset or invalid. 0
    disables the cap."""
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
    """`EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS`, or the default if unset or
    invalid."""
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
