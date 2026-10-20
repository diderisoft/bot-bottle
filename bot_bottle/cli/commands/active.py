"""active: list currently-running bot-bottle bottles."""

from __future__ import annotations

import os
import sys

from ...backend import enumerate_active_agents
from ..constants import PROG
from ..settings import settings_from_env

_ANSI_COLOR_CODES: dict[str, str] = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
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


def cmd_active(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} active\n")
        sys.stderr.write("\nList all currently-running bot-bottle bottles.\n")
        sys.stderr.write("Output: <backend>\\t<slug>\\t<label>\\t<services>\n")
        return 0

    active = enumerate_active_agents(settings_from_env())
    if not active:
        print("no active bot-bottle bottles", file=sys.stderr)
        return 0
    # One line per bottle: `<backend>\t<slug>\t<label>\t<services>`.
    # Tab-separated keeps the format stable for shell pipelines.
    for b in active:
        services = ",".join(b.services) if b.services else "-"
        display_name = f"{b.label} ({b.agent_name})" if b.label else b.agent_name
        colored_name = _ansi_label(display_name, b.color)
        print(f"{b.backend_name}\t{b.slug}\t{colored_name}\t{services}")
    return 0
