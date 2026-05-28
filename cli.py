#!/usr/bin/env python3
"""cli.py — entry point for the bot-bottle CLI. Run with --help (or
no args) for the command list."""

from __future__ import annotations

import sys

from bot_bottle.cli import main

if __name__ == "__main__":
    sys.exit(main())
