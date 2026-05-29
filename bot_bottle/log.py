"""Tiny logging wrappers. All output goes to stderr."""

from __future__ import annotations

import sys
from typing import NoReturn


def info(msg: str) -> None:
    print(f"bot-bottle: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"bot-bottle: warning: {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"bot-bottle: error: {msg}", file=sys.stderr)


class Die(SystemExit):
    """Raised by die() so callers (and tests) can distinguish a deliberate
    fatal exit from an unrelated SystemExit.

    Carries the human-facing message so a caller that suppressed stderr
    — e.g. the curses dashboard, whose alternate screen is wiped when the
    terminal is restored — can re-surface the reason after the fact."""

    def __init__(self, code: int = 1, message: str = "") -> None:
        super().__init__(code)
        self.message = message


def die(msg: str) -> NoReturn:
    error(msg)
    raise Die(1, msg)
