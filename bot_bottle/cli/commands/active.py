"""active: list running bottles: those the host controller holds, with their
session state, and anything else the backends find."""

from __future__ import annotations

import os
import sys

from ...backend import enumerate_active_agents
from ...contract.hostctl import BOTTLES_PATH
from ...control.hostctl import runner
from ...control.hostctl.client import HostControlClient
from ...lib.log import warn
from ..constants import PROG
from ..settings import settings_from_env
from ..table import print_table

_ANSI_COLOR_CODES: dict[str, str] = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
}
_ANSI_RESET = "\033[0m"

_HEADER = ("BACKEND", "BOTTLE", "NAME", "SERVICES", "SESSION")


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


def held_bottles() -> dict[str, dict[str, object]]:
    """The host controller's bottles by id; empty when it isn't running."""
    state = runner.running()
    if state is None:
        return {}
    try:
        body = HostControlClient(state, runner.cli_token()).get(BOTTLES_PATH)
    except runner.HostControlError as e:
        warn(f"can't list the host controller's bottles: {e}")
        return {}
    bottles = body.get("bottles")
    if not isinstance(bottles, list):
        return {}
    return {str(b["id"]): b for b in bottles if isinstance(b, dict) and "id" in b}


def _session_column(held: dict[str, object] | None) -> str:
    """`running` / `exited` for a session the host controller has, else `-`."""
    if held is None:
        return "-"
    session = held.get("session")
    running = isinstance(session, dict) and bool(session.get("running"))
    return "running" if running else "exited"


def cmd_active(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} active\n")
        sys.stderr.write("\nList all currently-running bot-bottle bottles: a table in a "
                         "terminal, piped one line each:\n")
        sys.stderr.write("<backend>\\t<slug>\\t<label>\\t<services>\\t<session>\n")
        sys.stderr.write("<session> is the host controller's session state; "
                         f"`{PROG} attach <slug>` joins a running one.\n")
        return 0

    held = held_bottles()
    active = enumerate_active_agents(settings_from_env())
    if not active and not held:
        print("no active bot-bottle bottles", file=sys.stderr)
        return 0
    # Per bottle: backend, slug, name, services, session; tab-separated when
    # piped, a table in a terminal.
    rows: list[tuple[str, ...]] = []
    colors: list[str] = []
    for b in active:
        services = ",".join(b.services) if b.services else "-"
        display_name = f"{b.label} ({b.agent_name})" if b.label else b.agent_name
        session = _session_column(held.pop(b.slug, None))
        rows.append((b.backend_name, b.slug, display_name, services, session))
        colors.append(b.color)
    # Held, but not seen by its backend.
    for slug, h in held.items():
        label, agent = str(h.get("label") or ""), str(h.get("agent") or "?")
        display_name = f"{label} ({agent})" if label else agent
        rows.append((str(h.get("backend") or "?"), slug, display_name, "-", _session_column(h)))
        colors.append("")
    print_table(_HEADER, rows,
                style=lambda r, c, cell: _ansi_label(cell, colors[r]) if c == 2 else cell)
    return 0
