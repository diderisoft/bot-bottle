"""Tiny logging wrappers. All output goes to stderr."""

from __future__ import annotations

import sys
from typing import NoReturn


def info(msg: str) -> None:
    print(f"claude-bottle: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"claude-bottle: warning: {msg}", file=sys.stderr)


class Die(SystemExit):
    """Raised by die() so callers (and tests) can distinguish a deliberate
    fatal exit from an unrelated SystemExit."""


def die(msg: str) -> NoReturn:
    print(f"claude-bottle: error: {msg}", file=sys.stderr)
    raise Die(1)
