"""Shared constants and tty helper for cli subcommands."""

from __future__ import annotations

import os
from pathlib import Path

from ..util import read_tty_line as read_tty_line

PROG = "cli.py"
USER_CWD = os.getcwd()
REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)
