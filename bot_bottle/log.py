"""Tiny logging wrappers. All output goes to stderr.

Two capabilities layer onto the bare wrappers (issue #252):

  - **Levels.** `debug` / `info` / `warn` / `error` carry an ordered
    severity. Output is gated by `BOT_BOTTLE_LOG_LEVEL` (debug | info |
    warn | error; default `info`). A message emits when its severity is
    at or above the threshold, so `debug` is silent by default and
    `error` always surfaces (nothing sits above it) — which keeps the
    fatal `die` path visible regardless of the configured level.

  - **Context.** Every wrapper takes an optional `context` mapping that
    renders as a parseable ` [k=v ...]` suffix (keys sorted; values with
    whitespace/quotes are quoted), so failures can be filtered and
    correlated instead of being flat strings.

With no `context` and the default level, output is byte-identical to the
original `bot-bottle: <msg>` / `bot-bottle: warning: <msg>` /
`bot-bottle: error: <msg>` lines — the 100+ existing call sites are
unaffected.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, NoReturn

# Ordered severities. Gaps left between values so intermediate levels
# can be added later without renumbering.
DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40

_LEVEL_NAMES: dict[str, int] = {
    "debug": DEBUG,
    "info": INFO,
    "warn": WARN,
    "warning": WARN,
    "error": ERROR,
}

# Default threshold when BOT_BOTTLE_LOG_LEVEL is unset or unrecognised.
_DEFAULT_THRESHOLD = INFO

_LOG_LEVEL_ENV = "BOT_BOTTLE_LOG_LEVEL"


def _threshold() -> int:
    """Resolve the active level threshold from the environment.

    Read per-call (not cached) so the level can be changed at runtime
    and so tests can patch `os.environ` without a reload. Unknown values
    fall back to the default rather than raising — logging must never be
    the thing that crashes the process."""
    raw = os.environ.get(_LOG_LEVEL_ENV, "")
    return _LEVEL_NAMES.get(raw.strip().lower(), _DEFAULT_THRESHOLD)


def _format_context(context: Mapping[str, object] | None) -> str:
    """Render a context mapping as a ` [k=v k2=v2]` suffix.

    Keys are sorted for stable, diffable output. Values that are empty or
    contain whitespace or a quote are wrapped in double quotes (with inner
    quotes escaped) so each `k=v` pair stays parseable. Empty/None context
    renders as the empty string."""
    if not context:
        return ""
    parts: list[str] = []
    for key in sorted(context):
        value = str(context[key])
        if value == "" or any(ch.isspace() for ch in value) or '"' in value:
            value = '"' + value.replace('"', '\\"') + '"'
        parts.append(f"{key}={value}")
    return " [" + " ".join(parts) + "]"


def _emit(
    level: int,
    label: str,
    msg: str,
    context: Mapping[str, object] | None,
) -> None:
    if level < _threshold():
        return
    prefix = f"{label}: " if label else ""
    sys.stderr.write(f"bot-bottle: {prefix}{msg}{_format_context(context)}\n")


def debug(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(DEBUG, "debug", msg, context)


def info(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(INFO, "", msg, context)


def warn(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(WARN, "warning", msg, context)


def error(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(ERROR, "error", msg, context)


class Die(SystemExit):
    """Raised by die() so callers (and tests) can distinguish a deliberate
    fatal exit from an unrelated SystemExit.

    Carries the human-facing message so a caller that suppressed stderr
    — e.g. the curses dashboard, whose alternate screen is wiped when the
    terminal is restored — can re-surface the reason after the fact."""

    def __init__(self, code: int = 1, message: str = "") -> None:
        super().__init__(code)
        self.message = message


def die(msg: str, *, context: Mapping[str, object] | None = None) -> NoReturn:
    error(msg, context=context)
    raise Die(1, msg)
