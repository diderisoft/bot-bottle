"""The one terminal view bot-bottle draws: a left column beside the bottle.

The guided `bot-bottle start`, the home menu (bare `bot-bottle`) and the
launch's stages all draw here, so a start goes from its questions to its
launch stages without the screen clearing. The left column is a step list
(`Step`): done steps collapse to one line with a ✓, the current one is
expanded and takes keys, a running one spins with its time, pending ones are
dim. A footer gives the keys.

`Screen` owns the alternate screen: frames are diffed and wrapped in
synchronized output, a resize redraws, and `suspend()` hands the normal
screen back (an editor, `setup`, an agent's terminal). `compose` lays out one
frame and is pure, for tests.

Only on a terminal (`usable`). `NO_COLOR` turns off color and the bottle; a
narrow terminal drops the bottle for a one-line header.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
import unicodedata
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TextIO

from . import logo

RESET = "\x1b[0m"
CURSOR = "▌"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Alternate screen and hidden cursor, and back. DEC 2026 synchronized output
# shows whole frames.
ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J"
LEAVE = "\x1b[?25h\x1b[?1049l"
SYNC_BEGIN, SYNC_END = "\x1b[?2026h", "\x1b[?2026l"
# Layout: a margin, the left column, a gap, then the bottle.
MARGIN = 2
GAP = 4
MIN_LEFT = 30
# Cursor blink period, in 0.1 s ticks.
BLINK_TICKS = 5
# Step title column width.
TITLE_WIDTH = 10

DONE, CURRENT, RUNNING, FAILED, PENDING = "done", "current", "running", "failed", "pending"


def usable() -> bool:
    """Whether to draw: the process's own stdin and stderr are terminals
    (not a pipe, and not a stand-in a caller swapped in)."""
    try:
        return (sys.stdin is sys.__stdin__ and os.isatty(sys.stdin.fileno())
                and os.isatty(sys.stderr.fileno())
                and os.environ.get("TERM", "dumb") != "dumb")
    except (OSError, ValueError, AttributeError):
        return False


def wants_color() -> bool:
    return not os.environ.get("NO_COLOR")


# --- the bottle ---

def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def _xterm256(r: int, g: int, b: int) -> int:
    """The nearest xterm 6x6x6 cube color."""
    def level(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * level(r) + 6 * level(g) + level(b)


def _sgr(hex6: str, *, background: bool, truecolor: bool) -> str:
    r, g, b = _rgb(hex6)
    if truecolor:
        return f"\x1b[{48 if background else 38};2;{r};{g};{b}m"
    return f"\x1b[{48 if background else 38};5;{_xterm256(r, g, b)}m"


def has_truecolor() -> bool:
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit")


def logo_rows(*, truecolor: bool, cursor: bool = True) -> list[str]:
    """The bottle as terminal rows (half blocks). `cursor=False` omits the
    prompt's `_`, for the blink."""
    art: list[str] = list(logo.ART)
    if not cursor:
        row, first, last = logo.CURSOR
        line = art[row]
        art[row] = line[:first] + logo.CURSOR_OFF * (last - first + 1) + line[last + 1:]
    if len(art) % 2:
        art.append("." * len(art[0]))
    rows: list[str] = []
    for top, bottom in zip(art[0::2], art[1::2]):
        pixels: list[str] = []
        for t, b in zip(top, bottom):
            tc, bc = logo.PALETTE.get(t), logo.PALETTE.get(b)
            if tc and bc:
                pixels.append(f"{_sgr(tc, background=False, truecolor=truecolor)}"
                             f"{_sgr(bc, background=True, truecolor=truecolor)}▀{RESET}")
            elif tc:
                pixels.append(f"{_sgr(tc, background=False, truecolor=truecolor)}▀{RESET}")
            elif bc:
                pixels.append(f"{_sgr(bc, background=False, truecolor=truecolor)}▄{RESET}")
            else:
                pixels.append(" ")
        rows.append("".join(pixels))
    return rows


ART_WIDTH = len(logo.ART[0])


# --- text ---

def strip(text: str) -> str:
    """`text` without escape sequences, for measuring width."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\x1b":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not text[j].isalpha():
                    j += 1
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _char_cells(ch: str) -> int:
    """Columns one character takes: 2 for wide (CJK, emoji), 0 for combining
    marks and controls, else 1."""
    if unicodedata.combining(ch) or not ch.isprintable():
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cells(text: str) -> int:
    """Columns plain `text` takes on a terminal."""
    return sum(_char_cells(ch) for ch in text)


def _cut(text: str, width: int) -> str:
    """Plain `text`'s longest prefix within `width` columns."""
    out, used = [], 0
    for ch in text:
        used += _char_cells(ch)
        if used > width:
            break
        out.append(ch)
    return "".join(out)


def fit(text: str, width: int) -> str:
    """Plain `text` cut to `width` columns with an ellipsis."""
    if width <= 0:
        return ""
    return text if cells(text) <= width else _cut(text, width - 1) + "…"


def clip(text: str, width: int) -> str:
    """Styled `text` cut to `width` columns, its escapes kept whole, and reset
    when cut, so a long line neither wraps on the terminal nor leaks a color."""
    if cells(strip(text)) <= width:
        return text
    out, used, i, styled = [], 0, 0, False
    while i < len(text):
        if text[i] == "\x1b":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not text[j].isalpha():
                    j += 1
            out.append(text[i:j + 1])
            styled = True
            i = j + 1
            continue
        used += _char_cells(text[i])
        if used > width:
            break
        out.append(text[i])
        i += 1
    return "".join(out) + (RESET if styled else "")


def input_lines(lead: str, text: str, width: int, *, max_rows: int = 6) -> list[str]:
    """A line being typed after the styled `lead`, with a cursor, wrapped
    within `width` columns under itself; past `max_rows` the last rows, so
    the cursor stays in view."""
    indent = cells(strip(lead))
    room = max(width - indent, 1)
    rows: list[str] = []
    line, used = "", 0
    for word in _words(text + CURSOR):
        size_ = cells(word)
        if used + size_ > room and line:
            rows.append(line)
            line, used = "", 0
            word = word.lstrip(" ") if word.strip() else word
            size_ = cells(word)
        while size_ > room:  # a word longer than a row breaks
            head = _cut(word, room) or word[0]
            rows.append(head)
            word = word[len(head):]
            size_ = cells(word)
        line += word
        used += size_
    rows.append(line)
    if len(rows) > max_rows:
        rows = rows[-max_rows:]
        rows[0] = "…" + rows[0][1:]
    pad = " " * indent
    return [clip((lead if n == 0 else pad) + row, width) for n, row in enumerate(rows)]


def _words(text: str) -> list[str]:
    """`text` split after each run of spaces, so typed spaces stay."""
    return re.findall(r"[^ ]+ *| +", text)


def wrap(text: str, width: int) -> list[str]:
    """Plain `text` wrapped to `width` (words, else hard breaks)."""
    import textwrap  # pylint: disable=import-outside-toplevel
    return textwrap.wrap(text, max(width, 8), break_long_words=True) or [""]


@dataclass
class Style:
    """SGR helpers that collapse to plain text without color."""

    color: bool = True

    def _sgr(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}{RESET}" if self.color and text else text

    def dim(self, text: str) -> str:
        return self._sgr("2", text)

    def bold(self, text: str) -> str:
        return self._sgr("1", text)

    def green(self, text: str) -> str:
        return self._sgr("32", text)

    def red(self, text: str) -> str:
        return self._sgr("31", text)

    def amber(self, text: str) -> str:
        return self._sgr("38;5;178", text)

    def accent(self, text: str) -> str:
        return self._sgr("38;5;173", text)

    def inverse(self, text: str) -> str:
        return self._sgr("7", text)


# --- steps ---

@dataclass
class Step:
    """One line of the step list, and the current step's body."""

    title: str
    value: str = ""
    state: str = PENDING
    body: list[str] = field(default_factory=list)  # styled lines, the current step's
    started: float = 0.0  # running: when it started

    def elapsed(self, now: float | None = None) -> str:
        return f"{(now or time.monotonic()) - self.started:.0f}s"


def step_lines(steps: list[Step], *, width: int, style: Style, frame: int = 0,
               now: float | None = None) -> list[str]:
    """The step list as styled lines, each at most `width` wide."""
    lines: list[str] = []
    for step in steps:
        title = step.title.ljust(TITLE_WIDTH)
        room = max(width - TITLE_WIDTH - 3, 4)
        if step.state == DONE:
            lines.append(f"{style.green('✓')} {style.dim(title)} {fit(step.value, room)}")
        elif step.state == FAILED:
            lines.append(f"{style.red('✗')} {title} {fit(step.value, room)}")
        elif step.state == RUNNING:
            spin = SPINNER[frame % len(SPINNER)]
            when = step.elapsed(now) if step.started else ""
            value = fit(step.value, max(room - len(when) - 2, 4))
            lines.append(f"{style.accent(spin)} {title} {value}  {style.dim(when)}".rstrip())
        elif step.state == CURRENT:
            lines.append(f"{style.accent('▸')} {style.bold(title)} {fit(step.value, room)}")
            lines.extend(f"    {line}" for line in step.body)
        else:
            lines.append(style.dim(f"· {title} {fit(step.value, room)}".rstrip()))
    return [line.rstrip() for line in lines]


def menu_lines(items: list[tuple[str, str]], cursor: int, *, width: int,
               style: Style, label_width: int = 18) -> list[str]:
    """A menu: `(label, detail)` per item, the cursor's marked with `›`."""
    lines = []
    for i, (label, detail) in enumerate(items):
        mark = style.accent("›") if i == cursor else " "
        text = label.ljust(label_width)
        text = style.bold(text) if i == cursor else text
        room = max(width - label_width - 3, 0)
        lines.append(f"{mark} {text} {style.dim(fit(detail, room))}".rstrip())
    return lines


# --- layout ---

def compose(left: list[str], art: list[str], *, columns: int, rows: int,  # pylint: disable=too-many-arguments,too-many-locals
            left_width: int, footer: str = "", header: str = "",
            align: str = "center") -> list[str]:
    """One frame: `left` beside `art`, the block centered, `footer` on the
    last row. Without room for the bottle it's dropped, and `header` (if any)
    goes on top instead. `align="top"` starts `left` level with the bottle's
    top (a step list that grows shouldn't jump); "center" centers it on it."""
    if art and (columns < MARGIN + MIN_LEFT + GAP + ART_WIDTH + MARGIN or rows < len(art) + 2):
        art = []
    if not art and header:
        left = [header, ""] + left
    room = columns - 2 * MARGIN - (GAP + ART_WIDTH if art else 0)
    width = max(min(left_width, room), 1)
    usable_rows = max(rows - (2 if footer else 0), 1)
    height = min(max(len(left), len(art)), max(usable_rows - 2, 1))
    block = width + (GAP + ART_WIDTH if art else 0)
    pad = " " * max((columns - block) // 2, MARGIN)
    top = max((len(art) - len(left)) // 2, 0) if art and align == "center" else 0
    lines = [""] * max((usable_rows - height) // 2, 1)
    for i in range(height):
        text = left[i - top] if 0 <= i - top < len(left) else ""
        text = clip(text, width)
        line = pad + text
        if art and i < len(art):
            line += " " * (width - cells(strip(text)) + GAP) + art[i]
        lines.append(line)
    if footer:
        lines += [""] * max(rows - 1 - len(lines), 0)
        lines.append(pad + footer)
    return lines


def size() -> tuple[int, int]:
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


# --- the alternate screen ---

class Screen:  # pylint: disable=too-many-instance-attributes
    """The alternate screen, drawn by diffed frames. Thread-safe."""

    def __init__(self, out: TextIO | None = None, *, color: bool | None = None) -> None:
        self.out = out or sys.stderr
        self.color = wants_color() if color is None else color
        self.style = Style(self.color)
        tc = has_truecolor()
        self.art = logo_rows(truecolor=tc) if self.color else []
        self.art_blink = logo_rows(truecolor=tc, cursor=False) if self.color else []
        self.frame = 0
        self.on = False
        self._previous: list[str] | None = None
        self._resized = threading.Event()
        self._old_winch: object = None
        self.lock = threading.RLock()
        if hasattr(signal, "SIGWINCH") and threading.current_thread() is threading.main_thread():
            self._old_winch = signal.signal(signal.SIGWINCH, self._on_resize)
        atexit.register(self._restore)

    def header(self) -> str:
        return self.style.accent("▣") + " " + self.style.bold("bot-bottle")

    def art_now(self) -> list[str]:
        return self.art_blink if (self.frame // BLINK_TICKS) % 2 else self.art

    def enter(self) -> None:
        with self.lock:
            if not self.on:
                self.out.write(ENTER)
                self.out.flush()
                self.on = True
                self._previous = None

    def leave(self) -> None:
        with self.lock:
            if self.on:
                self.out.write(LEAVE)
                self.out.flush()
                self.on = False

    def close(self) -> None:
        self.leave()
        if self._old_winch is not None:
            signal.signal(signal.SIGWINCH, self._old_winch)  # type: ignore[arg-type]
            self._old_winch = None

    @contextlib.contextmanager
    def suspend(self) -> Generator[None]:
        """The normal screen, for an editor, a command or an agent; drawing
        comes back on the next `draw`."""
        was_on = self.on
        self.leave()
        try:
            yield
        finally:
            if was_on:
                self.enter()

    def draw(self, left: list[str], *, footer: str = "", left_width: int = 72,
             align: str = "top") -> None:
        with self.lock:
            self.enter()
            if self._resized.is_set():
                self._resized.clear()
                self.out.write("\x1b[2J")
                self._previous = None
            columns, rows = size()
            frame = compose(left, self.art_now(), columns=columns, rows=rows,
                            left_width=left_width, footer=self.style.dim(footer),
                            header=self.header(), align=align)
            self.write_frame(frame)

    def write_frame(self, frame: list[str]) -> None:
        """Rewrite only the rows that changed since the last frame."""
        previous = self._previous or []
        changes = []
        for row, line in enumerate(frame):
            if self._previous is None or row >= len(previous) or previous[row] != line:
                changes.append(f"\x1b[{row + 1};1H\x1b[2K{line}")
        for row in range(len(frame), len(previous)):
            changes.append(f"\x1b[{row + 1};1H\x1b[2K")
        if changes:
            self.out.write(SYNC_BEGIN + "".join(changes) + SYNC_END)
            self.out.flush()
        self._previous = frame

    def invalidate(self) -> None:
        """Clear and redraw in full next time: something wrote to the
        terminal outside the frames, so the diff can't be trusted."""
        self._resized.set()

    def resized(self) -> bool:
        """Consume a pending resize (for callers drawing their own frames)."""
        if self._resized.is_set():
            self._resized.clear()
            self._previous = None
            self.out.write("\x1b[2J")
            return True
        return False

    def _on_resize(self, signum: int, frame: object) -> None:
        del signum, frame
        self._resized.set()

    def _restore(self) -> None:
        """At exit, never leave the terminal on the alternate screen."""
        if self.on:
            try:
                self.out.write(LEAVE)
                self.out.flush()
            except (OSError, ValueError):
                pass


# --- keys ---

_ESCAPES = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down",
            "OC": "right", "OD": "left", "[H": "home", "[F": "end", "[3~": "delete"}
_CONTROL = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace",
            "\x03": "ctrl-c", "\x04": "ctrl-d", "\t": "tab", "\x15": "ctrl-u"}


def decode_key(data: str) -> str:
    """A key name for the bytes one keypress sent: `up`, `enter`, `esc`, … or
    the character itself."""
    if data.startswith("\x1b"):
        return _ESCAPES.get(data[1:], "esc")
    return _CONTROL.get(data, data)


@contextlib.contextmanager
def keys_mode(fd: int | None = None) -> Generator[None]:
    """Unbuffered input without echo (cbreak) on stdin, restored after."""
    f = sys.stdin.fileno() if fd is None else fd
    saved = termios.tcgetattr(f)
    try:
        tty.setcbreak(f, termios.TCSANOW)  # not TCSAFLUSH: keep typed-ahead keys
        yield
    finally:
        termios.tcsetattr(f, termios.TCSADRAIN, saved)


def read_key(fd: int | None = None, *, timeout: float | None = None) -> str | None:
    """The next keypress (in `keys_mode`), or None after `timeout` seconds."""
    f = sys.stdin.fileno() if fd is None else fd
    if timeout is not None and not select.select([f], [], [], timeout)[0]:
        return None
    data = os.read(f, 1)
    if data == b"\x1b":
        while select.select([f], [], [], 0.03)[0]:
            data += os.read(f, 1)
            if data[-1:].isalpha() or data[-1:] == b"~":
                break
    elif data and data[0] >= 0xC0:  # the rest of a UTF-8 character
        need = 1 if data[0] < 0xE0 else 2 if data[0] < 0xF0 else 3
        data += os.read(f, need)
    return decode_key(data.decode("utf-8", errors="replace"))


def edit_line(text: str, key: str) -> str:
    """`text` after one key of line editing (printable, backspace, ctrl-u)."""
    if key == "backspace":
        return text[:-1]
    if key == "ctrl-u":
        return ""
    if len(key) == 1 and key.isprintable():
        return text + key
    return text


__all__ = [
    "CURRENT", "CURSOR", "DONE", "FAILED", "PENDING", "RUNNING", "Screen", "Step", "Style",
    "cells", "clip", "compose", "decode_key", "edit_line", "fit", "input_lines", "keys_mode",
    "logo_rows", "menu_lines", "read_key", "step_lines", "strip", "usable", "wants_color", "wrap",
]
