"""Telling an agent its supervise request was answered after it stopped
waiting.

The supervise server holds a request open for its response timeout (30s by
default), then answers `pending`. An agent told `pending` usually ends its turn
and waits for its operator, so a later answer would go unnoticed until someone
typed. When a request older than that wait is answered, a line saying so is
typed into the agent's session, as a console message would be.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from ...lib import log
from .console_terminal import SUBMIT_GAP_SECONDS
from .sessions import tmux

# The supervise server's default wait before it answers `pending`.
SUPERVISE_WAIT_SECONDS = 30
_MAX_NOTES = 300


def waited_out(arrived: str, now: datetime | None = None) -> bool:
    """Whether a request that arrived at `arrived` (ISO) is older than the
    supervise server's wait, so the agent was told `pending`."""
    try:
        when = datetime.fromisoformat(arrived)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - when).total_seconds() >= SUPERVISE_WAIT_SECONDS


def nudge_text(tool: str, outcome: str, notes: str = "") -> str:
    """One line: what was answered, and the operator's notes."""
    notes = " ".join(notes.split())[:_MAX_NOTES]
    return (f"bot-bottle: your {tool} request was {outcome} by the operator"
            f"{': ' + notes if notes else ''}. Carry on.")


def type_line(session: str, text: str) -> bool:
    """Type `text` into `session`'s pane, then Enter as its own keystroke (a TUI
    that reads a burst of input as a paste would take it for a newline)."""
    target = f"={session}:"
    if subprocess.run(tmux("has-session", "-t", f"={session}"),
                      capture_output=True, check=False).returncode != 0:
        return False
    typed = subprocess.run(tmux("send-keys", "-t", target, "-l", text),
                           capture_output=True, check=False)
    if typed.returncode != 0:
        return False
    time.sleep(SUBMIT_GAP_SECONDS)
    return subprocess.run(tmux("send-keys", "-t", target, "Enter"),
                          capture_output=True, check=False).returncode == 0


def nudge_if_waited(request: Any, outcome: str, notes: str = "") -> None:
    """Tell `request`'s agent it was answered, if it stopped waiting. Never
    raises: the answer already stands."""
    if not request.session or not waited_out(request.arrived):
        return
    try:
        if not type_line(request.session, nudge_text(request.tool, outcome, notes)):
            log.warn(f"couldn't tell {request.session} its request was {outcome}")
    except OSError as e:
        log.warn(f"couldn't tell {request.session} its request was {outcome}: {e}")


__all__ = ["SUPERVISE_WAIT_SECONDS", "nudge_if_waited", "nudge_text", "type_line",
           "waited_out"]
