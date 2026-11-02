"""A new label for a bottled agent (the console's `rename`, `bot-bottle
rename`): what people call it, in its metadata and its session record, so
`bot-bottle bots`, the console's listings and a resume all use it. Its
identity, and everything keyed by it (state dir, VM, transcripts, deploy
keys, commit trailers), stays."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...bottle_state import clean_label, rename_label
from ...lib import log

if TYPE_CHECKING:
    from .sessions import SessionManager


def rename(sessions: "SessionManager | None", identity: str,
           label: object) -> dict[str, object]:
    """`{"label": <cleaned>}`, or `{"error": …}` when the label won't do or
    nothing on this host records `identity`."""
    try:
        cleaned = clean_label(label)
    except ValueError as e:
        return {"error": str(e)}
    recorded = rename_label(identity, cleaned)
    in_session = sessions.relabel(identity, cleaned) if sessions is not None else False
    if not recorded and not in_session:
        return {"error": f"this host has no bottle {identity}"}
    log.info(f"bottle {identity}: renamed {cleaned!r}")
    return {"label": cleaned}


__all__ = ["rename"]
