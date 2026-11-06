"""Where this context's questions go: the terminal, or a full-screen view.

The prompts a command asks on the way (trust a bottle, confirm a host key,
type a secret, sign in) read the terminal line by line. A full-screen view
(home, the guided start) can't have them: they'd scroll it and garble its
frames. So, like `log.redirect` for output lines, a view installs a
`Prompter` for its context and each prompt asks through it:

    with prompt.redirect(view_prompter):
        run_command(...)

Without one (`current()` is None) every prompt asks the terminal as it always
has, so the commands run as themselves (and without a terminal) unchanged.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator, Sequence
from typing import Protocol


class Prompter(Protocol):
    """Asks the person at the screen. `context` is what they need to answer
    (the bottle's grants, the host key), shown above the question."""

    def confirm(self, question: str, *, context: Sequence[str] = (),
                default: bool = False) -> bool:
        """Yes or no; `default` on a bare Enter."""
        ...  # pylint: disable=unnecessary-ellipsis

    def ask(self, question: str, *, context: Sequence[str] = (), secret: bool = False) -> str:
        """A line of text ("" when they back out); `secret` hides what's typed."""
        ...  # pylint: disable=unnecessary-ellipsis

    def outside(self) -> contextlib.AbstractContextManager[None]:
        """The plain terminal for a while (a QR code, an editor), its prompts
        asking there."""
        ...  # pylint: disable=unnecessary-ellipsis


_PROMPTER: contextvars.ContextVar[Prompter | None] = contextvars.ContextVar(
    "bot_bottle_prompter", default=None
)


@contextlib.contextmanager
def redirect(prompter: Prompter | None) -> Generator[None]:
    """Ask this context's questions through `prompter` (None: the terminal)."""
    token = _PROMPTER.set(prompter)
    try:
        yield
    finally:
        _PROMPTER.reset(token)


def current() -> Prompter | None:
    """This context's prompter, or None when prompts ask the terminal."""
    return _PROMPTER.get()


@contextlib.contextmanager
def outside() -> Generator[None]:
    """The plain terminal for the body of the `with`: the view steps aside and
    prompts ask the terminal. Nothing to do without a view."""
    prompter = _PROMPTER.get()
    if prompter is None:
        yield
        return
    with prompter.outside(), redirect(None):
        yield


__all__ = ["Prompter", "current", "outside", "redirect"]
