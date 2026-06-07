"""list: list available agents or active bottles."""

from __future__ import annotations

import argparse
import os
import sys

from ..backend import enumerate_active_agents
from ..manifest import Manifest
from ._common import PROG, USER_CWD

_ANSI_COLOR_CODES: dict[str, str] = {
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright-black": "\033[90m",
    "bright-red": "\033[91m",
    "bright-green": "\033[92m",
    "bright-yellow": "\033[93m",
    "bright-blue": "\033[94m",
    "bright-magenta": "\033[95m",
    "bright-cyan": "\033[96m",
    "bright-white": "\033[97m",
}
_ANSI_RESET = "\033[0m"


def _ansi_label(text: str, color: str) -> str:
    if not color:
        return text
    if not sys.stdout.isatty():
        return text
    term = os.environ.get("TERM", "")
    if term in ("dumb", ""):
        return text
    code = _ANSI_COLOR_CODES.get(color)
    if not code:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} list", add_help=True)
    parser.add_argument("scope", choices=["available", "active"])
    args = parser.parse_args(argv)

    if args.scope == "available":
        manifest = Manifest.resolve(USER_CWD)
        for name in manifest.agents.keys():
            print(name)
        return 0

    # `active` enumerates every backend (docker + smolmachines)
    # so smolmachines bottles aren't hidden behind the env var.
    active = enumerate_active_agents()
    if not active:
        print("no active bot-bottle bottles", file=sys.stderr)
        return 0
    # One line per bottle: `<backend>\t<slug>\t<label>\t<services>`.
    # Tab-separated keeps the format stable for shell pipelines.
    for b in active:
        services = ",".join(b.services) if b.services else "-"
        display_name = b.label if b.label else b.agent_name
        colored_name = _ansi_label(display_name, b.color)
        print(f"{b.backend_name}\t{b.slug}\t{colored_name}\t{services}")
    return 0
