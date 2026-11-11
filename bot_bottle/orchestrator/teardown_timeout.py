"""How long a launch waits for the orchestrator to tear a bottle down: an env
override or the default, never a stored setting.
"""

from __future__ import annotations

import os

TEARDOWN_TIMEOUT_ENV = "BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS"
DEFAULT_TEARDOWN_TIMEOUT_SECONDS = 30.0


def resolve_teardown_timeout() -> float:
    """How long a launch waits for the orchestrator to tear a bottle down:
    `BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS`, else
    `DEFAULT_TEARDOWN_TIMEOUT_SECONDS` (30 s).
    """
    raw = os.environ.get(TEARDOWN_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TEARDOWN_TIMEOUT_SECONDS


__all__ = [
    "resolve_teardown_timeout",
    "TEARDOWN_TIMEOUT_ENV",
    "DEFAULT_TEARDOWN_TIMEOUT_SECONDS",
]
