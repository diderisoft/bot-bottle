"""Launch progress for the terminal (PRD 0094): one line per stage instead
of the job's whole log.

Each stage (`Reading the bottle`, `Getting images`, …) is one line that
updates while it runs, then ends with a green ✓ and how long it took. On a
wide enough terminal the stages sit in a panel next to the bot-bottle
bottle; otherwise they're plain lines. Every log line still goes to a
per-launch log file. Warnings and errors are also shown, above the
progress, because they're meant for the user. On failure, the failing stage,
the log's last lines and the log path are printed. ``verbose`` streams every
log line as before. `NO_COLOR` turns off color and the logo.
"""

from __future__ import annotations

import collections
import os
import shutil
import sys
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
# Log lines the user should see even when the rest is hidden.
_SHOWN = ("bot-bottle: warning:", "bot-bottle: error:")
# The panel: stages on the left, the logo after a gap.
_LEFT_WIDTH = 50
_GAP = 2
_RESET = "\x1b[0m"


def _home_relative(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def launch_log_path(name: str) -> Path:
    """A new launch log's path. Prunes the oldest launch logs first, so at
    most ``KEEP_LAUNCH_LOGS`` remain (counting the new one)."""
    logs = bot_bottle_root() / "logs"
    prune_launch_logs(logs, keep=KEEP_LAUNCH_LOGS - 1)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return logs / f"launch-{slugify(name) or 'bottle'}-{stamp}.log"


def prune_launch_logs(logs: Path, *, keep: int) -> None:
    """Delete all but the ``keep`` newest `launch-*.log` files. Other files
    in the directory are left alone, and a file that can't be removed is
    skipped."""
    try:
        found = sorted(logs.glob("launch-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in found[max(keep, 0):]:
        try:
            old.unlink()
        except OSError:
            pass


# -- color and the logo ------------------------------------------------------------


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def _xterm256(r: int, g: int, b: int) -> int:
    """The nearest color in the xterm 6x6x6 cube."""
    def level(v: int) -> int:
        return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40
    return 16 + 36 * level(r) + 6 * level(g) + level(b)


def _sgr(hex6: str, *, background: bool, truecolor: bool) -> str:
    r, g, b = _rgb(hex6)
    if truecolor:
        return f"\x1b[{48 if background else 38};2;{r};{g};{b}m"
    return f"\x1b[{48 if background else 38};5;{_xterm256(r, g, b)}m"


def logo_rows(*, truecolor: bool) -> list[str]:
    """The bottle as terminal rows (half blocks, two pixels per cell), each
    `logo.ART` columns wide."""
    art: list[str] = list(logo.ART)
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


class _Row:
    """One left-panel line: its plain text (for width) and how it's drawn."""

    def __init__(self, plain: str, drawn: str | None = None) -> None:
        self.plain = plain
        self.drawn = plain if drawn is None else drawn


class LaunchProgress:  # pylint: disable=too-many-instance-attributes
    """Draws stages, keeps the log. Thread-safe: the spinner redraws from
    its own thread."""

    def __init__(self, log_path: Path, *, verbose: bool = False,
                 out: TextIO | None = None, tty: bool | None = None,
                 panel: bool | None = None, color: bool | None = None) -> None:
        self.log_path = log_path
        self.verbose = verbose
        self.out = out or sys.stderr
        self.tty = self.out.isatty() if tty is None else tty
        self.color = (self.tty and not os.environ.get("NO_COLOR")) if color is None else color
        live = self.tty and not verbose
        if panel is None:
            columns = shutil.get_terminal_size((80, 24)).columns
            panel = live and self.color and columns >= _LEFT_WIDTH + _GAP + len(logo.ART[0])
        self.panel = panel
        truecolor = os.environ.get("COLORTERM", "") in ("truecolor", "24bit")
        self._logo = logo_rows(truecolor=truecolor) if panel else []
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("a", encoding="utf-8")
        self._tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
        self._since_stage: list[str] = []
        self._rows: list[_Row] = []  # finished stages (panel mode)
        self._title = ""
        self._name = ""
        self._started = 0.0
        self._drawn = 0  # panel lines on screen below which the cursor sits
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Set while a question is on screen: nothing may redraw over it.
        self._paused = False
        self._spinner: threading.Thread | None = None

    # -- events ----------------------------------------------------------------

    def line(self, text: str) -> None:
        with self._lock:
            self._log.write(text + "\n")
            self._log.flush()
            self._tail.append(text)
            self._since_stage.append(text)
            if self.verbose or text.startswith(_SHOWN):
                self._print_above(text)

    def stage(self, name: str, title: str) -> None:
        with self._lock:
            if name and name == self._name and self._title:
                # The same stage, more specific (e.g. "Starting bot-bottle
                # services" turning out to be a rebuild): retitle in place.
                self._log.write(f"== {title}\n")
                self._title = title
                self._draw()
                return
            self._finish("✓")
            self._name = name
            self._log.write(f"== {title}\n")
            self._title, self._started = title, time.monotonic()
            self._since_stage = []
            self._paused = False
            self._draw()
        if self.tty and not self.verbose and self._spinner is None:
            self._spinner = threading.Thread(target=self._spin, daemon=True)
            self._spinner.start()

    def done(self, message: str = "") -> None:
        with self._lock:
            self._finish("✓")
            if message:
                row = _Row(f"✓ {message}", f"{self._mark('✓')} {message}")
                if self.panel:
                    self._rows.append(row)
                    self._draw()
                else:
                    self.out.write(row.drawn + "\n")
            if not self.verbose:
                # Under the panel, full width: the path is longer than a column.
                text = f"  launch log: {_home_relative(self.log_path)}"
                self.out.write((f"\x1b[2m{text}{_RESET}" if self.color else text) + "\n")
            self.out.flush()
        self.close()

    def fail(self, message: str) -> None:
        with self._lock:
            self._finish("✗")
            if not self.verbose:
                for text in self._tail:
                    if not text.startswith(_SHOWN):
                        self.out.write(f"  {text}\n")
            self.out.write(f"{message}\n  full log: {self.log_path}\n")
            self.out.flush()
        self.close()

    def since_stage(self) -> list[str]:
        """The log lines of the current stage (a launch's preflight summary,
        shown before asking to confirm)."""
        with self._lock:
            return list(self._since_stage)

    def pause(self) -> None:
        """Clear the progress and stop redrawing it, so a prompt can use the
        terminal. The next stage resumes drawing."""
        with self._lock:
            self._paused = True
            if self.tty and not self.verbose:
                self._clear()

    def close(self) -> None:
        self._stop.set()
        if self._spinner is not None:
            self._spinner.join(timeout=1)
        with self._lock:
            if not self._log.closed:
                self._log.close()

    # -- drawing (lock held) ----------------------------------------------------

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self._started:.0f}s"

    def _mark(self, mark: str) -> str:
        if not self.color:
            return mark
        return f"\x1b[{32 if mark == '✓' else 31}m{mark}{_RESET}"

    def _frame(self, frame: str) -> str:
        return f"\x1b[38;5;173m{frame}{_RESET}" if self.color else frame

    def _clear(self) -> None:
        """Erase what's drawn: the panel, or the current progress line."""
        if self._drawn:
            self.out.write(f"\x1b[{self._drawn}F\x1b[J")
            self._drawn = 0
        elif self._title:
            self.out.write("\r\x1b[K")
        self.out.flush()

    def _draw(self, frame: str = "…") -> None:
        if self.verbose or self._paused or not self.tty:
            return
        if self.panel:
            self._draw_panel(frame)
        elif self._title:
            self.out.write(f"\r\x1b[K{self._frame(frame)} {self._title}  {self._elapsed()}")
            self.out.flush()

    def _draw_panel(self, frame: str) -> None:
        rows = list(self._rows)
        if self._title:
            plain = f"{frame} {self._title}  {self._elapsed()}"
            rows.append(_Row(plain, f"{self._frame(frame)} {self._title}  {self._elapsed()}"))
        height = max(len(rows), len(self._logo))
        lines = []
        for i in range(height):
            row = rows[i] if i < len(rows) else _Row("")
            plain = row.plain[:_LEFT_WIDTH]
            drawn = row.drawn if len(row.plain) <= _LEFT_WIDTH else plain
            art = self._logo[i] if i < len(self._logo) else ""
            pad = " " * (_LEFT_WIDTH - len(plain) + _GAP)
            lines.append(f"\x1b[2K{drawn}{pad}{art}")
        up = f"\x1b[{self._drawn}F" if self._drawn else ""
        self.out.write(up + "\n".join(lines) + "\n")
        self.out.flush()
        self._drawn = height

    def _finish(self, mark: str) -> None:
        if not self._title:
            return
        plain = f"{mark} {self._title}  {self._elapsed()}"
        drawn = f"{self._mark(mark)} {self._title}  {self._elapsed()}"
        self._title = ""
        if self.panel and not self.verbose:
            self._rows.append(_Row(plain, drawn))
            self._draw()
            return
        clear = "\r\x1b[K" if self.tty and not self.verbose else ""
        self.out.write(f"{clear}{drawn}\n")
        self.out.flush()

    def _print_above(self, text: str) -> None:
        if self.tty and not self.verbose and not self._paused and (self._drawn or self._title):
            self._clear()
            self.out.write(text + "\n")
            self._draw()
        else:
            self.out.write(text + "\n")
        self.out.flush()

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.1):
            with self._lock:
                if self._title:
                    self._draw(_SPINNER[i % len(_SPINNER)])
            i += 1


__all__ = ["LaunchProgress", "launch_log_path", "logo_rows"]
