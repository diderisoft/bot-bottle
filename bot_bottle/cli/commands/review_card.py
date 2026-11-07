"""The guided start's review card: `suggest.review`'s lines as columns.

  new      api.open-meteo.com   fetch forecasts (GET)
  basic    16 hosts             Package registries and public code (GET)
  forge    gitea.dideric.is     as didericis

Labels and what each line is get a column each; the rest is the detail with
what it permits in brackets, and the secret it carries, wrapped under the
detail when it doesn't fit. New lines and secrets are amber, read-only lines
dim, a forge that isn't connected amber too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import screen

if TYPE_CHECKING:
    from ...suggest.review import Line

_MAX_LABEL = 16
_MAX_WHAT = 26


def _columns(lines: "list[Line]", width: int) -> tuple[int, int]:
    label = min(max((len(x.label) for x in lines), default=0), _MAX_LABEL) + 2
    what = min(max((len(x.what) for x in lines), default=0), _MAX_WHAT) + 2
    what = min(what, max(width - label - 14, 8))
    return label, what


def _tone(style: screen.Style, tone: str, text: str) -> str:
    if tone == "read":
        return style.dim(text)
    if tone == "warn":
        return style.amber(text)
    return text


def card(lines: "list[Line]", *, width: int, style: screen.Style) -> list[str]:
    """`lines` as the review card, each row at most `width` wide."""
    label_w, what_w = _columns(lines, width)
    room = max(width - label_w - what_w, 8)
    out: list[str] = []
    for line in lines:
        label = screen.fit(line.label, label_w - 2).ljust(label_w)
        label = style.amber(label) if line.tone == "new" else style.dim(label)
        if not line.what:  # a section header (the details list)
            out.append(label + style.dim(screen.fit(line.detail, width - label_w)))
            continue
        what = screen.fit(line.what, what_w - 2).ljust(what_w)
        detail = line.detail or ""
        tail = (" " if detail else "") + f"({line.access})" if line.access else ""
        secret = f" with {line.secret}" if line.secret else ""
        if len(detail) + len(tail) + len(secret) <= room:
            text = _tone(style, line.tone, detail + tail) + style.amber(secret)
            out.append(label + _tone(style, line.tone, what) + text)
            continue
        if line.tone == "warn":  # what to do about it: all of it, wrapped
            wrapped = screen.wrap(detail + tail, room)
            out.append(label + what + style.amber(wrapped[0]))
            out += [" " * (label_w + what_w) + style.amber(w) for w in wrapped[1:]]
            continue
        body = screen.fit(detail, max(room - len(tail), 4)) + tail
        out.append(label + _tone(style, line.tone, what) + _tone(style, line.tone, body))
        if secret:
            out.append(" " * (label_w + what_w) + style.amber(screen.fit(secret.strip(), room)))
    return out


def window(rows: list[str], offset: int, height: int, *,
           style: screen.Style) -> tuple[list[str], int]:
    """`height` of `rows` from `offset` (clamped, and returned), with a line
    saying how many are above or below when they don't all fit."""
    if len(rows) <= height:
        return rows, 0
    height = max(height - 1, 1)
    offset = max(0, min(offset, len(rows) - height))
    shown = rows[offset:offset + height]
    above, below = offset, len(rows) - offset - len(shown)
    marks = [f"↑ {above} more" if above else "", f"↓ {below} more" if below else ""]
    return shown + [style.dim(" · ".join(m for m in marks if m) + " · ↑↓ scroll")], offset


__all__ = ["card", "window"]
