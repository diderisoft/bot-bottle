"""A new label for a bottled agent (the console's `rename`, `bot-bottle
rename`): what people call it, kept once, in its `bottled_agents` row, so
`bot-bottle bots`, the console's listings, its session and a resume all use
it. Its identity, and everything keyed by it (state dir, VM, transcripts,
deploy keys, commit trailers), stays."""

from __future__ import annotations

from ...bottle_state import clean_label, rename_label
from ...lib import log


def rename(identity: str, label: object) -> dict[str, object]:
    """`{"label": <cleaned>}`, or `{"error": …}` when the label won't do or
    nothing on this host records `identity`."""
    try:
        cleaned = clean_label(label)
    except ValueError as e:
        return {"error": str(e)}
    if not rename_label(identity, cleaned):
        return {"error": f"this host has no bottle {identity}"}
    log.info(f"bottle {identity}: renamed {cleaned!r}")
    return {"label": cleaned}


__all__ = ["rename"]
