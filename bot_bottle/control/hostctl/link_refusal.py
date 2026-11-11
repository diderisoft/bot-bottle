"""The console refusing this host: it no longer knows its node id.

The console closes a connection from a node that isn't bound to one of its
hosts with application code 1 and the reason `unregistered host`. That's what
a host sees once its account is deleted, or the host is removed from it.
iroh hands the close back as text, e.g.

    closed by peer: unregistered host (code 1)

The link only reports it: `unpaired` in the link's state
(`pairing.read_status`, what `status`, `doctor` and home read) and a long wait
before trying again. It never acts on its own: the pairing, the iroh key and
the link setting stay as they are, so a `bot-bottle pair` (to the same
console, or a new account) is all it takes, and the next connection that's
accepted clears it. Only the pinned console can
send it: `run` checks the console's node id before serving.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ...lib import log
from .pairing import read_status, write_status

# What the console sends (bb_console/iroh_node.py: _CLOSE_REFUSED, its reason).
REFUSED_CODE = 1
UNREGISTERED_REASON = "unregistered host"
# How long a refused host waits before asking again, instead of the backoff.
UNPAIRED_BACKOFF_SECONDS = 600.0
UNPAIRED_ERROR = ("this console doesn't know this host anymore (its account was deleted, "
                  "or the host was removed); run `bot-bottle pair` to pair it again")


def is_unregistered(reason: str | None) -> bool:
    """Whether iroh's close `reason` is the console's `unregistered host`."""
    if not reason:
        return False
    text = reason.strip()
    return "by peer" in text and text.endswith(f"{UNREGISTERED_REASON} (code {REFUSED_CODE})")


def close_reason(conn: Any) -> str | None:
    """Why `conn` closed, or None (still open, never connected, or unreadable)."""
    if conn is None:
        return None
    with contextlib.suppress(Exception):
        reason = conn.close_reason()
        return reason if isinstance(reason, str) else None
    return None


def refused_delay(conn: Any) -> float:
    """After the link went down: when the console refused this host, record
    it and return the long wait; else 0 (the usual backoff applies)."""
    if not is_unregistered(close_reason(conn)):
        return 0.0
    if not read_status().get("unpaired"):
        log.warn(f"remote: {UNPAIRED_ERROR}")
    write_status(connected=False, unpaired=True, error=UNPAIRED_ERROR)
    return UNPAIRED_BACKOFF_SECONDS


def unpaired() -> bool:
    """Whether the console last refused this host as unregistered."""
    return bool(read_status().get("unpaired"))


__all__ = ["REFUSED_CODE", "UNPAIRED_BACKOFF_SECONDS", "UNPAIRED_ERROR",
           "UNREGISTERED_REASON", "close_reason", "is_unregistered", "refused_delay",
           "unpaired"]
