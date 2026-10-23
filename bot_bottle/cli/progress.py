"""Launch progress for the terminal (PRD 0094): one line per stage instead
of the job's whole log.

Each stage (`Reading the bottle`, `Getting images`, …) is one line that
updates in place while it runs, then ends with ✓ and how long it took.
Every log line still goes to a per-launch log file. Warnings and errors are
also shown above the progress line, because they're meant for the user. On
failure, the failing stage, the log's last lines and the log path are
printed. ``verbose`` streams every log line as before.
"""

from __future__ import annotations

import collections
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from ..paths import bot_bottle_root
from ..util import slugify

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TAIL_LINES = 12
# Log lines the user should see even when the rest is hidden.
_SHOWN = ("bot-bottle: warning:", "bot-bottle: error:")


def launch_log_path(name: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return bot_bottle_root() / "logs" / f"launch-{slugify(name) or 'bottle'}-{stamp}.log"


class LaunchProgress:
    """Draws stages, keeps the log. Thread-safe: the spinner redraws from
    its own thread."""

    def __init__(self, log_path: Path, *, verbose: bool = False,
                 out: TextIO | None = None, tty: bool | None = None) -> None:
        self.log_path = log_path
        self.verbose = verbose
        self.out = out or sys.stderr
        self.tty = self.out.isatty() if tty is None else tty
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("a", encoding="utf-8")
        self._tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
        self._since_stage: list[str] = []
        self._title = ""
        self._started = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
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
        del name
        with self._lock:
            self._finish("✓")
            self._log.write(f"== {title}\n")
            self._title, self._started = title, time.monotonic()
            self._since_stage = []
            self._draw()
        if self.tty and not self.verbose and self._spinner is None:
            self._spinner = threading.Thread(target=self._spin, daemon=True)
            self._spinner.start()

    def done(self, message: str = "") -> None:
        with self._lock:
            self._finish("✓")
            if message:
                self.out.write(f"✓ {message}\n")
        self.close()

    def fail(self, message: str) -> None:
        with self._lock:
            self._finish("✗")
            if not self.verbose:
                for text in self._tail:
                    if not text.startswith(_SHOWN):
                        self.out.write(f"  {text}\n")
            self.out.write(f"{message}\n  full log: {self.log_path}\n")
        self.close()

    def since_stage(self) -> list[str]:
        """The log lines of the current stage (a launch's preflight summary,
        shown before asking to confirm)."""
        with self._lock:
            return list(self._since_stage)

    def pause(self) -> None:
        """Clear the progress line so a prompt can use the terminal."""
        with self._lock:
            if self.tty and self._title and not self.verbose:
                self.out.write("\r\x1b[K")
                self.out.flush()

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

    def _draw(self, frame: str = "…") -> None:
        if self.verbose or not self._title:
            return
        if self.tty:
            self.out.write(f"\r\x1b[K{frame} {self._title}  {self._elapsed()}")
            self.out.flush()

    def _finish(self, mark: str) -> None:
        if not self._title:
            return
        clear = "\r\x1b[K" if self.tty and not self.verbose else ""
        self.out.write(f"{clear}{mark} {self._title}  {self._elapsed()}\n")
        self.out.flush()
        self._title = ""

    def _print_above(self, text: str) -> None:
        if self.tty and self._title and not self.verbose:
            self.out.write(f"\r\x1b[K{text}\n")
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


__all__ = ["LaunchProgress", "launch_log_path"]
