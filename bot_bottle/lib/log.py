"""Logging wrappers, writing to stderr unless redirected (`redirect`).

- Levels: `debug`, `info`, `warn`, `error`, gated by `BOT_BOTTLE_LOG_LEVEL`
  (default `info`). `error`, and so `die`, always shows.
- Context: an optional mapping rendered as a parseable ` [k=v ...]` suffix.

Lines look like `bot-bottle: <msg>`, `bot-bottle: warning: <msg>`, `bot-bottle:
error: <msg>`.
"""

from __future__ import annotations

import contextvars
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Mapping, NoReturn

# Ordered severities, spaced for future levels.
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

# Used when BOT_BOTTLE_LOG_LEVEL is unset or unknown.
_DEFAULT_THRESHOLD = INFO

_LOG_LEVEL_ENV = "BOT_BOTTLE_LOG_LEVEL"

# This context's output sink; None means stderr. Host-controller jobs route
# lines to their event stream.
_SINK: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "bot_bottle_log_sink", default=None
)


@contextmanager
def redirect(sink: Callable[[str], None]) -> Generator[None]:
    """Send this context's output lines to `sink` instead of stderr."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def redirected() -> bool:
    """True when this context's output goes to a sink rather than stderr."""
    return _SINK.get() is not None


def current_sink() -> Callable[[str], None]:
    """This context's sink, as a callable usable from another thread."""
    sink = _SINK.get()
    return sink if sink is not None else _stderr_line


def _stderr_line(line: str) -> None:
    sys.stderr.write(line + "\n")


def write_line(line: str) -> None:
    """Write one line (no newline) to this context's sink."""
    sink = _SINK.get()
    if sink is None:
        sys.stderr.write(line + "\n")
    else:
        sink(line)


def _threshold() -> int:
    """The level threshold from the env, read per call. Unknown values fall
    back to the default."""
    raw = os.environ.get(_LOG_LEVEL_ENV, "")
    return _LEVEL_NAMES.get(raw.strip().lower(), _DEFAULT_THRESHOLD)


def debug_enabled() -> bool:
    """True when `BOT_BOTTLE_LOG_LEVEL` lets `debug` lines through."""
    return _threshold() <= DEBUG


def _format_context(context: Mapping[str, object] | None) -> str:
    """A context mapping as a ` [k=v k2=v2]` suffix, keys sorted. Empty values
    or ones with whitespace or quotes are double-quoted. "" for empty context."""
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
    write_line(f"bot-bottle: {prefix}{msg}{_format_context(context)}")


# Stage markers travel through the same sinks as log lines.
_STAGE_MARK = "\x1fstage\x1f"


def stage(name: str, title: str) -> None:
    """Mark the start of a launch stage (`prepare`, `image`, `start`,
    `provision`). A job sink records a `stage` event; without a sink it logs
    like `info`."""
    if _SINK.get() is None:
        info(title)
    else:
        write_line(f"{_STAGE_MARK}{name}\x1f{title}")


def parse_stage(line: str) -> tuple[str, str] | None:
    """`(name, title)` if ``line`` is a stage marker, else None."""
    if not line.startswith(_STAGE_MARK):
        return None
    name, _, title = line[len(_STAGE_MARK):].partition("\x1f")
    return name, title


def debug(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(DEBUG, "debug", msg, context)


def info(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(INFO, "", msg, context)


def warn(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(WARN, "warning", msg, context)


def error(msg: str, *, context: Mapping[str, object] | None = None) -> None:
    _emit(ERROR, "error", msg, context)


class Die(SystemExit):
    """Raised by die(), distinct from other SystemExits. Carries the message so
    a caller that hid stderr can show it afterwards."""

    def __init__(self, code: int = 1, message: str = "") -> None:
        super().__init__(code)
        self.message = message


def die(msg: str, *, context: Mapping[str, object] | None = None) -> NoReturn:
    error(msg, context=context)
    raise Die(1, msg)
