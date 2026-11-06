"""A `lib.prompt.Prompter` that asks on a full-screen view, inline.

The view (home, the guided start) says how to draw a prompt with `show`:
its lines under what the view already has, and the footer's keys. `show(None,
"")` means the prompt is answered: drop its lines, without drawing.

  untrusted bottle: ./main  (~/Code/app/.bot-bottle/bottles/main.md)

    Beyond the basic bottle, it lets the agent:
      reach api.example.com (GET)

  Trust this bottle on this host?  y/N
  y yes · n no
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence

from . import screen

# Rows a prompt leaves the view above it: the header, the step list, the footer.
_KEPT_ROWS = 12


class ViewPrompter:
    """Asks through `show` and `key` (the view's next key, blocking)."""

    def __init__(self, show: Callable[[list[str] | None, str], None],  # pylint: disable=too-many-arguments
                 key: Callable[[], str | None], *, width: Callable[[], int],
                 style: screen.Style,
                 suspend: Callable[[], contextlib.AbstractContextManager[None]]) -> None:
        self.show = show
        self.key = key
        self.width = width
        self.style = style
        self.suspend = suspend

    def confirm(self, question: str, *, context: Sequence[str] = (),
                default: bool = False) -> bool:
        lines = self._context(context) + [f"{question}  "
                                          + self.style.dim("Y/n" if default else "y/N")]
        try:
            while True:
                self.show(lines, "y yes · n no")
                key = self.key()
                if key in ("y", "Y"):
                    return True
                if key in ("n", "N", "esc", "q"):
                    return False
                if key == "enter":
                    return default
                if key == "ctrl-c":
                    raise KeyboardInterrupt
        finally:
            self.show(None, "")

    def ask(self, question: str, *, context: Sequence[str] = (), secret: bool = False) -> str:
        text = ""
        lead = f"{question} {self.style.accent('›')} "
        try:
            while True:
                shown = "•" * len(text) if secret else text
                self.show(self._context(context) + screen.input_lines(lead, shown, self.width()),
                          "enter ok · esc back")
                key = self.key()
                if key == "enter":
                    return text.strip()
                if key == "esc":
                    return ""
                if key == "ctrl-c":
                    raise KeyboardInterrupt
                text = screen.edit_line(text, key or "")
        finally:
            self.show(None, "")

    def outside(self) -> contextlib.AbstractContextManager[None]:
        return self.suspend()

    def _context(self, context: Sequence[str]) -> list[str]:
        """`context` wrapped to the view, cut to the rows it has, then a gap."""
        width = self.width()
        lines = [row for line in context for row in _wrap_indented(line, width)]
        room = max(screen.size()[1] - _KEPT_ROWS, 4)
        if len(lines) > room:
            lines = lines[:room - 1] + [self.style.dim(f"… {len(lines) - room + 1} more lines")]
        return lines + [""] if lines else []


def _wrap_indented(line: str, width: int) -> list[str]:
    """`line` wrapped to `width`, each row under its first's indent."""
    text = line.rstrip()
    if not text:
        return [""]
    indent = " " * (len(text) - len(text.lstrip(" ")))
    return [indent + row for row in screen.wrap(text.lstrip(" "), max(width - len(indent), 10))]


__all__ = ["ViewPrompter"]
