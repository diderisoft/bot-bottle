"""Forge tokens that stopped working for good, for the console to show.

A GitHub authorization that can't be renewed (its refresh token expired or was
revoked) or a Gitea token the forge refuses (401/403) won't fix itself: every
renewal or launch would fail the same way. This keeps one alert per credential
(the host secret's name), raised when that happens and cleared when the
credential works again or is replaced (`forge connect`). The console gets them
with `host_stats` (`forge_alerts`) and shows each once per failure state.
Network trouble is never an alert: callers warn and retry.

In-process only: a host controller restart forgets them, and the next renewal
or launch raises them again.
"""

from __future__ import annotations

import threading
import time

GITHUB_REAUTHORIZE = "github_reauthorize"
GITEA_TOKEN_REFUSED = "gitea_token_refused"
KINDS = (GITHUB_REAUTHORIZE, GITEA_TOKEN_REFUSED)

_lock = threading.Lock()
_alerts: dict[str, dict[str, object]] = {}


def raise_alert(key: str, *, kind: str, forge: str, message: str) -> bool:
    """Alert that credential `key` (a host secret's name) stopped working.
    True when it's new or its message changed, so a caller warns once per
    failure state, not on every retry."""
    if kind not in KINDS:
        raise ValueError(f"unknown forge alert kind {kind!r}")
    with _lock:
        old = _alerts.get(key)
        if old is not None and old["kind"] == kind and old["message"] == message:
            return False
        _alerts[key] = {"key": key, "kind": kind, "forge": forge, "message": message,
                        "since": old["since"] if old is not None else time.time()}
        return True


def clear(key: str) -> bool:
    """The credential works again (or was replaced). True if there was an alert."""
    with _lock:
        return _alerts.pop(key, None) is not None


def current() -> list[dict[str, object]]:
    """The alerts standing now, oldest first."""
    with _lock:
        return sorted((dict(a) for a in _alerts.values()),
                      key=lambda a: float(str(a["since"])))


def reset() -> None:
    """Forget every alert (tests)."""
    with _lock:
        _alerts.clear()


__all__ = ["GITEA_TOKEN_REFUSED", "GITHUB_REAUTHORIZE", "KINDS", "clear", "current",
           "raise_alert", "reset"]
