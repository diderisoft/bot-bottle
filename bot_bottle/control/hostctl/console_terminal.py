"""The console's view of one session: a `tmux attach` under its own PTY,
relayed on an iroh uni stream (ADR 0007).

The stream starts with the console's agent id and a newline, then terminal
bytes. Keystrokes and sizes arrive on the control stream through `ConsoleLink`.
The client starts read-only and can type only if the agent's manifest sets
`remote.writable`. tmux sizes to the most recently active client.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ...lib import log
from .attach import _LOGIN_TTY, _set_size
from .sessions import SessionError, SessionManager

# Bounds on one console message.
MAX_INPUT_BYTES = 64 * 1024
# Most lines one `history` request may get.
HISTORY_LINES = 3000
MAX_HISTORY_CHARS = 2_000_000
COLS_RANGE = (20, 400)
ROWS_RANGE = (5, 200)


class _SendStream(Protocol):
    async def write_all(self, data: bytes, /) -> None: ...
    async def finish(self) -> None: ...


class ConsoleTerminal:
    """One agent's terminal, relayed to the console until `close`."""

    def __init__(self, sessions: SessionManager, session_id: str, agent_id: str, *,
                 writable_allowed: bool) -> None:
        self._sessions = sessions
        self._session = session_id
        self._agent_id = agent_id
        self.writable = writable_allowed
        self._master = -1
        self._proc: subprocess.Popen[bytes] | None = None
        self._pump: threading.Thread | None = None
        self._closed = threading.Event()

    async def start(self, open_stream: Callable[[], Awaitable[_SendStream]],
                    cols: int = 120, rows: int = 40) -> None:
        """Open the console stream and attach to the session."""
        tmux_bin = shutil.which("tmux")
        if tmux_bin is None:
            raise SessionError("tmux is not installed")
        stream = await open_stream()
        await stream.write_all(self._agent_id.encode() + b"\n")
        argv = self._sessions.attach_argv(self._session, read_only=True)
        argv[0] = tmux_bin
        master, slave = os.openpty()
        _set_size(master, cols, rows)
        tty = os.ttyname(slave)
        self._proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c", _LOGIN_TTY, *argv],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            env={"TERM": "xterm-256color", "LANG": "C.UTF-8"},
        )
        os.close(slave)
        self._master = master
        if self.writable:
            # The client attached read-only; the agent allows remote typing.
            await asyncio.to_thread(self._make_writable, tty)
        loop = asyncio.get_running_loop()
        self._pump = threading.Thread(target=self._relay, args=(master, stream, loop),
                                      daemon=True, name=f"console-term-{self._session}")
        self._pump.start()

    def _make_writable(self, tty: str) -> None:
        # tmux registers the client a moment after it starts.
        for _ in range(20):
            if self._closed.wait(0.1):
                return
            if self._client_listed(tty):
                self._sessions.set_read_only(tty, False)
                return
        log.warn(f"console terminal for {self._session}: client never appeared; read-only")
        self.writable = False

    def _client_listed(self, tty: str) -> bool:
        from .sessions import tmux  # pylint: disable=import-outside-toplevel
        out = subprocess.run(tmux("list-clients", "-F", "#{client_tty}"),
                             capture_output=True, text=True, check=False).stdout
        return tty in out.split()

    def _relay(self, master: int, stream: _SendStream, loop: asyncio.AbstractEventLoop) -> None:
        """Copy PTY output to the console, awaiting each write so a slow
        console gets backpressure."""
        try:
            while data := os.read(master, 65536):
                asyncio.run_coroutine_threadsafe(stream.write_all(data), loop).result()
        except Exception:  # pylint: disable=broad-exception-caught  # PTY or stream closed
            pass
        if self._proc is not None:  # reap the tmux client
            if self._proc.poll() is None:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        try:
            asyncio.run_coroutine_threadsafe(stream.finish(), loop).result(timeout=5)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        # The relay owns the PTY master; closing it after the last read means
        # no thread reuses the fd mid-read.
        self._closed.set()
        os.close(master)

    def write(self, data: bytes) -> bool:
        """Type `data` into the session, if the agent allows remote typing."""
        if not self.writable or self._master < 0 or self._closed.is_set():
            return False
        os.write(self._master, data[:MAX_INPUT_BYTES])
        return True

    def resize(self, cols: int, rows: int) -> None:
        if self._master >= 0 and not self._closed.is_set():
            _set_size(self._master, cols, rows)

    def history(self, lines: int) -> tuple[str, int, int]:
        """Up to `lines` (at most HISTORY_LINES) of scrolled-off history, with
        the history's size and limit."""
        if self._closed.is_set():
            return "", 0, 0
        text, size, limit = self._sessions.history(self._session,
                                                   max(1, min(lines, HISTORY_LINES)))
        if len(text) > MAX_HISTORY_CHARS:
            # Whole lines only; a mid-line cut could split an escape code.
            text = text[-MAX_HISTORY_CHARS:]
            text = text[text.find("\n") + 1:]
        return text, size, limit

    def close(self) -> None:
        """Detach (the agent keeps running). Non-blocking: the relay closes the
        PTY once the tmux client is gone."""
        if self._closed.is_set():
            return
        self._closed.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        if self._pump is None and self._master >= 0:  # never started relaying
            os.close(self._master)
        self._master = -1


def valid_size(cols: Any, rows: Any) -> tuple[int, int] | None:
    """(cols, rows) if both are ints in range, else None."""
    if type(cols) is not int or type(rows) is not int:  # pylint: disable=unidiomatic-typecheck
        return None
    if COLS_RANGE[0] <= cols <= COLS_RANGE[1] and ROWS_RANGE[0] <= rows <= ROWS_RANGE[1]:
        return cols, rows
    return None


__all__ = ["ConsoleTerminal", "HISTORY_LINES", "MAX_INPUT_BYTES", "valid_size"]
