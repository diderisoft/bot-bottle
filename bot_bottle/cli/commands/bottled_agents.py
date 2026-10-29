"""bottled-agents (`bots` for short): list bottled agents, running and stopped.

A bottled agent is an agent definition running in its bottles under one
identity: it runs, stops and resumes (ADR 0009). Running ones come from the
backends and the host controller, stopped ones from their kept state.

    bot-bottle bottled-agents [--running | --stopped]
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from ...backend import enumerate_active_agents
from ...bottle_state import BottleMetadata, bottle_state_dir, is_preserved, read_metadata
from ...paths import bot_bottle_root
from ..constants import PROG
from ..settings import settings_from_env
from ..table import print_table
from .active import _ansi_label, held_bottles

_USAGE = f"""usage: {PROG} bottled-agents [--running | --stopped]   (or: {PROG} bots)

List bottled agents, running ones first, then stopped ones most recently used
first; a table in a terminal. Piped, each is a tab-separated line:
<bottled agent>\\t<agent>\\t<label>\\t<bottles>\\t<state>\\t<terminal>\\t<last used>\\t<backend>
(`-` for an empty field).

  <state>     running, stopped, or `left over` (stopped without ending
              cleanly; `{PROG} cleanup` removes those)
  <terminal>  for a running one, whether its terminal is open (`{PROG} attach`)

`{PROG} resume <bottled agent>` starts a stopped one again.
"""

_HEADER = ("BOTTLED AGENT", "AGENT", "LABEL", "BOTTLES", "STATE", "TERMINAL", "LAST USED",
           "BACKEND")


def _last_used(path: Path) -> float:
    """When the state dir last changed: its own mtime or its newest entry's."""
    times = [path.stat().st_mtime]
    for child in path.iterdir():
        try:
            times.append(child.stat().st_mtime)
        except OSError:
            continue
    return max(times)


def stopped_bottles(live: set[str]) -> list[tuple[BottleMetadata, float]]:
    """Resumable bottles not in `live`, with when each was last used, newest
    first."""
    state_root = bot_bottle_root() / "state"
    if not state_root.is_dir():
        return []
    found: list[tuple[BottleMetadata, float]] = []
    for child in state_root.iterdir():
        if not child.is_dir() or child.name in live:
            continue
        metadata = read_metadata(child.name)
        if metadata is None:
            continue
        try:
            found.append((metadata, _last_used(bottle_state_dir(child.name))))
        except OSError:
            continue
    return sorted(found, key=lambda m: m[1], reverse=True)


def when(ts: float) -> str:
    """A timestamp as local `YYYY-MM-DD HH:MM`."""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def _terminal(held: dict[str, object] | None) -> str:
    """`open` / `exited` for a terminal the host controller has, else blank."""
    if held is None:
        return ""
    session = held.get("session")
    return "open" if isinstance(session, dict) and session.get("running") else "exited"


def _label(label: str, identity: str) -> str:
    # A label that only repeats the identity adds nothing.
    return "" if label == identity else label


def _running() -> tuple[list[tuple[str, ...]], list[str]]:
    """Rows for the running bottled agents, and each one's colour."""
    held = held_bottles()
    rows: list[tuple[str, ...]] = []
    colors: list[str] = []
    for b in enumerate_active_agents(settings_from_env()):
        metadata = read_metadata(b.slug)
        bottles = ",".join(metadata.bottle_names) if metadata else ""
        rows.append((b.slug, b.agent_name, _label(b.label, b.slug), bottles, "running",
                     _terminal(held.pop(b.slug, None)), "", b.backend_name))
        colors.append(b.color)
    # Held by the host controller, but not seen by its backend.
    for slug, h in held.items():
        rows.append((slug, str(h.get("agent") or "?"), _label(str(h.get("label") or ""), slug),
                     "", "running", _terminal(h), "", str(h.get("backend") or "")))
        colors.append("")
    return rows, colors


def _stopped(running: set[str]) -> list[tuple[str, ...]]:
    return [(m.identity, m.agent_name or "?", _label(m.label, m.identity),
             ",".join(m.bottle_names), "stopped" if is_preserved(m.identity) else "left over",
             "", when(used), m.backend)
            for m, used in stopped_bottles(running)]


def stopped_table() -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """The header and rows `bots --stopped` shows (without the terminal
    column, blank for stopped ones), for pickers such as `resume`'s."""
    running, _ = _running()
    keep = [i for i, name in enumerate(_HEADER) if name != "TERMINAL"]
    rows = [tuple(r[i] for i in keep) for r in _stopped({r[0] for r in running})]
    return tuple(_HEADER[i] for i in keep), rows


def cmd_bottled_agents(argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in argv) or set(argv) - {"--running", "--stopped"} \
            or len(argv) > 1:
        sys.stderr.write(_USAGE)
        return 0 if any(a in ("-h", "--help") for a in argv) else 2
    show_running = "--stopped" not in argv
    show_stopped = "--running" not in argv
    running, colors = _running()
    rows = running if show_running else []
    rows = rows + (_stopped({r[0] for r in running}) if show_stopped else [])
    if not rows:
        which = {"--running": "running ", "--stopped": "stopped "}.get(argv[0] if argv else "", "")
        print(f"no {which}bottled agents", file=sys.stderr)
        return 0
    print_table(_HEADER, rows, drop_empty=True,
                style=lambda r, c, cell: _ansi_label(cell, colors[r])
                if c == 0 and show_running and r < len(colors) else cell)
    if show_stopped and any(r[4] != "running" for r in rows):
        sys.stdout.flush()
        print(f"\nresume a stopped one with `{PROG} resume <bottled agent>`", file=sys.stderr)
    return 0


__all__ = ["cmd_bottled_agents", "stopped_bottles", "stopped_table"]
