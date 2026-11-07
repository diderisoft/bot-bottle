"""The guided start's review, talking with the architect (PRD 0118): the
conversation's lines above the gist, and each edit the proposal makes to
another bottle, accepted or skipped one at a time.

  › use the gitea mirror instead
  Builds on ./main. Adds the Gitea forge to ./main so the agent can push.

  edit ./main  + didericis/bot-bottle (push)
               the project's forge
  ✓    bot-bottle-dev: edited (a new version)

`a` accepts the first pending edit (written then), `x` skips it, `r` replies.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .. import screen

if TYPE_CHECKING:
    from ...suggest.proposal import Proposal
    from ...suggest.turns import Turn

EDIT_KEYS = "a accept · x skip · r reply · d diff · q cancel"
EDIT_CONNECT_KEYS = "c connect · x skip · r reply · d diff · q cancel"
_LABEL = 5
_SHOWN_TURNS = 3


def turn_lines(turns: "list[Turn]", width: int, style: screen.Style) -> list[str]:
    """Each reply so far, a line each (the last few), dim."""
    shown = [style.dim(screen.fit(f"› {t.reply}", width)) for t in turns[-_SHOWN_TURNS:]]
    if len(turns) > _SHOWN_TURNS:
        shown.insert(0, style.dim(f"  ({len(turns) - _SHOWN_TURNS} earlier)"))
    return shown


def hanging(label: str, text: str, width: int, *, paint_label: Callable[[str], str],
            paint: Callable[[str], str], label_width: int = _LABEL) -> list[str]:
    """`text` after `label`, its wrapped lines under the text."""
    rows = screen.wrap(text, max(width - label_width, 8)) or [""]
    return ([paint_label(label.ljust(label_width)) + paint(rows[0])]
            + [" " * label_width + paint(r) for r in rows[1:]])


def edit_lines(proposal: "Proposal", decided: list[str], width: int,
               style: screen.Style) -> list[str]:
    """The edits decided so far, then each pending one: its bottle and what
    it adds, its why under that. The first pending is the one `a`/`x` act on."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest import edits
    lines: list[str] = []
    for line in decided:
        lines += hanging("✓", line, width, paint_label=style.green, paint=style.dim)
    for i, edit in enumerate(proposal.edits):
        label = f"edit {edit.bottle}"
        indent = min(len(label) + 2, 24)
        lines += hanging(label, edits.summary(edit), width, label_width=indent,
                         paint_label=style.amber if i == 0 else style.dim,
                         paint=str if i == 0 else style.dim)
        if edit.why:
            lines.append(" " * indent + style.dim(screen.fit(edit.why, width - indent)))
    return lines


def diff_lines(proposal: "Proposal", dirs: list[tuple[Path, bool]], width: int,
               style: screen.Style) -> list[str]:
    """The first pending edit as a diff of its bottle's file."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest import edits
    if not proposal.edits:
        return []
    edit = proposal.edits[0]
    out = [style.amber(f"edit {edit.bottle}")]
    for line in edits.diff(edit, dirs, proposal.forges):
        text = screen.fit(line, width)
        if line.startswith("+") and not line.startswith("+++"):
            out.append(style.green(text))
        elif line.startswith("-") and not line.startswith("---"):
            out.append(style.red(text))
        else:
            out.append(style.dim(text))
    return out + [""]


def decided_line(name: str, project: bool, existed: bool) -> str:
    """What an accepted edit says once written."""
    if project:
        return f"{name}: written in the repo and trusted; commit it"
    return f"{name}: {'edited' if existed else 'made'} (a new version)"


__all__ = ["EDIT_CONNECT_KEYS", "EDIT_KEYS", "decided_line", "diff_lines", "edit_lines",
           "hanging", "turn_lines"]
