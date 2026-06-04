"""Shared constants and tty helper for cli subcommands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROG = "cli.py"
USER_CWD = os.getcwd()
REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)


def read_tty_line() -> str:
    """Mirror `IFS= read -r REPLY </dev/tty`. Falls back to stdin."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")
