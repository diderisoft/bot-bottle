"""Launch progress: one line per stage instead of the job's whole log.

On a terminal it draws on the alternate screen: stages with a spinner, then a ✓
and time, beside the bottle. Frames are diffed and wrapped in synchronized
output; a resize redraws. When done, failed or asking something, it leaves the
alternate screen and prints a summary: stages, times, warnings, the log path
and, on failure, the log's tail.

Without a terminal, or with `verbose`, it prints plain lines. Every log line
goes to a per-launch log file. `NO_COLOR` turns off color and the bottle.
"""

from __future__ import annotations

import atexit
import collections
import os
import shutil
import signal
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import TextIO

from ..paths import bot_bottle_root
from ..util import slugify
from . import logo

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
KEEP_LAUNCH_LOGS = 50
_TAIL_LINES = 12
# Log lines shown even when the rest is hidden.
_SHOWN = ("bot-bottle: warning:", "bot-bottle: error:")
# Most lines the launch screen gives to warnings and errors.
_NOTE_LINES = 8
_RESET = "\x1b[0m"
# Layout, centered: a fixed-width stage column, a gap, then the bottle.
_MARGIN = 2
_GAP = 4
_MIN_STAGE_WIDTH = 30
_STAGE_WIDTH = 40
# Cursor blink period, in 0.1 s spinner ticks.
_BLINK_TICKS = 5
# Alternate screen and hidden cursor, and back. DEC 2026 synchronized output
# shows whole frames.
_ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J"
_LEAVE = "\x1b[?25h\x1b[?1049l"
_SYNC_BEGIN, _SYNC_END = "\x1b[?2026h", "\x1b[?2026l"


def _home_relative(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def launch_log_path(name: str) -> Path:
    """A new launch log's path, after pruning to `KEEP_LAUNCH_LOGS`."""
    logs = bot_bottle_root() / "logs"
    prune_launch_logs(logs, keep=KEEP_LAUNCH_LOGS - 1)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return logs / f"launch-{slugify(name) or 'bottle'}-{stamp}.log"


def prune_launch_logs(logs: Path, *, keep: int) -> None:
    """Delete all but the `keep` newest `launch-*.log` files; unremovable ones
    are skipped."""
    try:
        found = sorted(logs.glob("launch-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in found[max(keep, 0):]:
        try:
            old.unlink()
        except OSError:
            pass


# --- color and the bottle ---


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
        cells: list[str] = []
        for t, b in zip(top, bottom):
            tc, bc = logo.PALETTE.get(t), logo.PALETTE.get(b)
            if tc and bc:
                cells.append(f"{_sgr(tc, background=False, truecolor=truecolor)}"
                             f"{_sgr(bc, background=True, truecolor=truecolor)}▀{_RESET}")
            elif tc:
                cells.append(f"{_sgr(tc, background=False, truecolor=truecolor)}▀{_RESET}")
            elif bc:
                cells.append(f"{_sgr(bc, background=False, truecolor=truecolor)}▄{_RESET}")
            else:
                cells.append(" ")
        rows.append("".join(cells))
    return rows


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


class _Stage:  # pylint: disable=too-few-public-methods
    def __init__(self, title: str) -> None:
        self.title = title
        self.started = time.monotonic()
        self.seconds: float | None = None  # set when finished
        self.failed = False

    def elapsed(self) -> str:
        end = self.started + self.seconds if self.seconds is not None else time.monotonic()
        return f"{end - self.started:.0f}s"


class LaunchProgress:  # pylint: disable=too-many-instance-attributes
    """Draws stages and keeps the log. Thread-safe; the spinner runs on its own
    thread."""

    def __init__(self, log_path: Path, *, verbose: bool = False,
                 out: TextIO | None = None, tty: bool | None = None,
                 screen: bool | None = None, color: bool | None = None) -> None:
        self.log_path = log_path
        self.verbose = verbose
        self.out = out or sys.stderr
        self.tty = self.out.isatty() if tty is None else tty
        self.color = (self.tty and not os.environ.get("NO_COLOR")) if color is None else color
        # Full-screen drawing, else plain lines.
        self.screen = (self.tty and not verbose) if screen is None else screen
        truecolor = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
        drawn = self.screen and self.color
        self._logo = logo_rows(truecolor=truecolor) if drawn else []
        self._logo_blink = logo_rows(truecolor=truecolor, cursor=False) if drawn else []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("a", encoding="utf-8")
        self._tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
        self._since_stage: list[str] = []
        self._notes: list[str] = []  # warnings and errors
        self._stages: list[_Stage] = []
        self._name = ""
        self._frame = 0
        self._on_screen = False  # inside the alternate screen
        self._previous: list[str] | None = None  # last frame drawn, for diffing
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = False  # a question is on the normal screen
        self._spinner: threading.Thread | None = None
        self._resized = threading.Event()
        self._old_winch: object = None
        if self.screen and hasattr(signal, "SIGWINCH") \
                and threading.current_thread() is threading.main_thread():
            self._old_winch = signal.signal(signal.SIGWINCH, self._on_resize)
        if self.screen:
            atexit.register(self._restore)

    # --- events ---

    def line(self, text: str) -> None:
        with self._lock:
            self._log.write(text + "\n")
            self._log.flush()
            self._tail.append(text)
            self._since_stage.append(text)
            shown = text.startswith(_SHOWN)
            if self.screen and not self._paused:
                if shown:
                    self._notes.append(text)
                    self._draw()
            elif self.verbose or shown:
                self._write_line(text)

    def stage(self, name: str, title: str) -> None:
        with self._lock:
            self._log.write(f"== {title}\n")
            current = self._current()
            if name and name == self._name and current is not None:
                # Same stage, more specific: retitle it.
                current.title = title
                self._draw()
                return
            self._finish(failed=False)
            self._name = name
            self._stages.append(_Stage(title))
            self._since_stage = []
            if self._paused:
                self._paused = False
            self._draw()
        if self.tty and not self.verbose and self._spinner is None:
            self._spinner = threading.Thread(target=self._spin, daemon=True)
            self._spinner.start()

    def done(self, message: str = "") -> None:
        with self._lock:
            self._finish(failed=False)
            self._leave_screen()
            self._summary(ok_line=message)
            if not self.verbose:
                self._write_line(self._dim(f"  launch log: {_home_relative(self.log_path)}"))
        self.close()

    def fail(self, message: str) -> None:
        with self._lock:
            self._finish(failed=True)
            self._leave_screen()
            self._summary()
            if not self.verbose:
                for text in self._tail:
                    if not text.startswith(_SHOWN):
                        self._write_line(f"  {text}")
            self._write_line(f"{message}\n  full log: {self.log_path}")
        self.close()

    def since_stage(self) -> list[str]:
        """The current stage's log lines (the preflight summary before a
        confirmation)."""
        with self._lock:
            return list(self._since_stage)

    def pause(self) -> None:
        """Give the normal screen back for a prompt; drawing resumes at the
        next stage."""
        with self._lock:
            self._paused = True
            if self.screen:
                self._leave_screen()
            elif self.tty and self._current() is not None and not self.verbose:
                self.out.write("\r\x1b[K")
                self.out.flush()

    def close(self) -> None:
        if self._old_winch is not None:
            signal.signal(signal.SIGWINCH, self._old_winch)  # type: ignore[arg-type]
            self._old_winch = None
        self._stop.set()
        if self._spinner is not None:
            self._spinner.join(timeout=1)
        with self._lock:
            self._leave_screen()
            if not self._log.closed:
                self._log.close()

    # --- drawing (lock held) ---

    def _current(self) -> _Stage | None:
        if self._stages and self._stages[-1].seconds is None:
            return self._stages[-1]
        return None

    def _finish(self, *, failed: bool) -> None:
        current = self._current()
        if current is None:
            return
        current.seconds = time.monotonic() - current.started
        current.failed = failed
        if not self.screen:
            clear = "\r\x1b[K" if self.tty and not self.verbose else ""
            self.out.write(f"{clear}{self._stage_text(current)}\n")
            self.out.flush()
        else:
            self._draw()

    def _mark(self, mark: str) -> str:
        if not self.color:
            return mark
        return f"\x1b[{32 if mark == '✓' else 31}m{mark}{_RESET}"

    def _spinner_frame(self) -> str:
        frame = _SPINNER[self._frame % len(_SPINNER)] if self.tty else "…"
        return f"\x1b[38;5;173m{frame}{_RESET}" if self.color else frame

    def _dim(self, text: str) -> str:
        return f"\x1b[2m{text}{_RESET}" if self.color else text

    def _stage_text(self, stage: _Stage, width: int | None = None) -> str:
        title = stage.title
        if width is not None and len(title) + 6 > width:
            title = title[:max(width - 7, 1)] + "…"
        if stage.seconds is None:
            return f"{self._spinner_frame()} {title}  {self._dim(stage.elapsed())}"
        mark = self._mark("✗" if stage.failed else "✓")
        return f"{mark} {title}  {self._dim(stage.elapsed())}"

    def _write_line(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def _summary(self, ok_line: str = "") -> None:
        """What the launch did, on the normal screen."""
        if not self.screen:
            if ok_line:
                self._write_line(f"{self._mark('✓')} {ok_line}")
            return
        for stage in self._stages:
            self._write_line(self._stage_text(stage))
        for note in self._notes:
            self._write_line(note)
        if ok_line:
            self._write_line(f"{self._mark('✓')} {ok_line}")

    # --- the alternate screen ---

    def _enter_screen(self) -> None:
        if not self._on_screen:
            self.out.write(_ENTER)
            self._on_screen = True
            self._previous = None

    def _leave_screen(self) -> None:
        if self._on_screen:
            self.out.write(_LEAVE)
            self.out.flush()
            self._on_screen = False

    def _restore(self) -> None:
        """At exit, never leave the terminal on the alternate screen."""
        if self._on_screen:
            try:
                self.out.write(_LEAVE)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def _on_resize(self, signum: int, frame: object) -> None:
        del signum, frame
        self._resized.set()  # the handler only sets a flag; drawing takes the lock

    def _layout(self) -> list[str]:
        """The whole screen for the current size: stages and bottle, centered."""
        columns, rows = _size()
        art = self._logo_blink if (self._frame // _BLINK_TICKS) % 2 else self._logo
        art_width = len(logo.ART[0])
        if art and (columns < _MARGIN + _MIN_STAGE_WIDTH + _GAP + art_width + _MARGIN
                    or rows < len(art) + 2):
            art = []  # no room for the bottle
        room = columns - 2 * _MARGIN - (_GAP + art_width if art else 0)
        stage_width = max(min(_STAGE_WIDTH, room), 1)
        left = [self._stage_text(s, stage_width) for s in self._stages]
        if self._notes:
            left.append("")
            left.extend(self._note_lines(stage_width))
        height = min(max(len(left), len(art)), max(rows - 2, 1))
        block_width = stage_width + (_GAP + art_width if art else 0)
        pad = " " * max((columns - block_width) // 2, _MARGIN)
        # Stages level with the bottle's body.
        top = max((len(art) - len(left)) // 2, 0) if art else 0
        lines = [""] * max((rows - height) // 2, 1)
        for i in range(height):
            text = left[i - top] if 0 <= i - top < len(left) else ""
            line = pad + text
            if art and i < len(art):
                line += " " * (stage_width - len(_strip(text)) + _GAP) + art[i]
            lines.append(line)
        return lines

    def _note_lines(self, width: int) -> list[str]:
        """The newest warnings and errors, wrapped to `width` (not cut off),
        at most _NOTE_LINES lines, in amber."""
        lines: list[str] = []
        for note in reversed(self._notes):
            wrapped = textwrap.wrap(note, max(width, 10)) or [""]
            if lines and len(lines) + len(wrapped) > _NOTE_LINES:
                break
            lines = wrapped + lines
        lines = lines[:_NOTE_LINES]
        return [f"\x1b[38;5;178m{line}{_RESET}" if self.color else line for line in lines]

    def _draw(self) -> None:
        if not self.tty or self.verbose or self._paused:
            return
        if not self.screen:
            current = self._current()
            if current is not None:
                self.out.write(f"\r\x1b[K{self._stage_text(current)}")
                self.out.flush()
            return
        self._enter_screen()
        if self._resized.is_set():
            self._resized.clear()
            self.out.write("\x1b[2J")
            self._previous = None
        frame = self._layout()
        previous = self._previous or []
        changes = []
        for row, line in enumerate(frame):
            if self._previous is None or row >= len(previous) or previous[row] != line:
                changes.append(f"\x1b[{row + 1};1H\x1b[2K{line}")
        for row in range(len(frame), len(previous)):
            changes.append(f"\x1b[{row + 1};1H\x1b[2K")
        if changes:
            self.out.write(_SYNC_BEGIN + "".join(changes) + _SYNC_END)
            self.out.flush()
        self._previous = frame

    def _spin(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._frame += 1
                if self._current() is not None:
                    self._draw()  # spinner, and the cursor every _BLINK_TICKS


def _strip(text: str) -> str:
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


__all__ = ["LaunchProgress", "launch_log_path", "logo_rows"]
