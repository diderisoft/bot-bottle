"""One remote viewer of a session: a `tmux attach` under its own PTY, pumped
to and from a WebSocket (PRD 0092).

Binary frames carry terminal bytes both ways. Text frames carry JSON control
messages: from the viewer `{"type": "resize", "cols", "rows"}` and
`{"type": "writable", "value": bool}`; to the viewer `{"type": "exit",
"status", "signal"}` when the agent exits, or `{"type": "detached"}` when the
viewer's tmux client ends for another reason. The viewer starts read-only;
its keystrokes are dropped until it becomes writable, which needs the
session's agent to allow it.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import struct
import subprocess
import sys
import termios
import threading
from typing import BinaryIO

from ...lib import websocket as ws
from .sessions import SessionError, SessionManager

# Make the PTY the child's controlling terminal (so resizes reach tmux), then exec.
_LOGIN_TTY = "import os, sys; os.login_tty(0); os.execv(sys.argv[1], sys.argv[1:])"


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


class Viewer:
    def __init__(self, manager: SessionManager, session_id: str, rfile: BinaryIO,
                 wfile: BinaryIO, *, writable_allowed: bool) -> None:
        self._manager = manager
        self._session = session_id
        self._rfile, self._wfile = rfile, wfile
        self._writable_allowed = writable_allowed
        self._writable = False
        self._send_lock = threading.Lock()

    def _send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            ws.write_frame(self._wfile, opcode, payload)

    def run(self, cols: int = 80, rows: int = 24) -> None:
        """Pump until the viewer leaves or its tmux client ends."""
        tmux_bin = shutil.which("tmux")
        if tmux_bin is None:
            raise SessionError("tmux is not installed")
        argv = self._manager.attach_argv(self._session, read_only=True)
        argv[0] = tmux_bin
        master, slave = os.openpty()
        _set_size(master, cols, rows)
        tty = os.ttyname(slave)
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c", _LOGIN_TTY, *argv],
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            env={"TERM": "xterm-256color", "LANG": "C.UTF-8"},
        )
        os.close(slave)
        output = threading.Thread(target=self._pump_output, args=(master, proc), daemon=True)
        output.start()
        try:
            self._pump_input(master, tty)
        except (EOFError, OSError, ws.WebSocketError):
            pass
        finally:
            if proc.poll() is None:
                proc.terminate()
            output.join(timeout=5)
            os.close(master)

    def _pump_output(self, master: int, proc: subprocess.Popen[bytes]) -> None:
        try:
            while data := os.read(master, 65536):
                self._send(ws.BINARY, data)
        except OSError:
            pass  # the tmux client closed its side
        proc.wait()
        exit_status = self._manager.wait_exit(self._session, timeout=2)
        end = ({"type": "exit", "status": exit_status.status, "signal": exit_status.signal}
               if exit_status else {"type": "detached"})
        try:
            self._send(ws.TEXT, json.dumps(end).encode())
            self._send(ws.CLOSE, b"")
        except OSError:
            pass

    def _pump_input(self, master: int, tty: str) -> None:
        while True:
            opcode, payload = ws.read_frame(self._rfile)
            if opcode == ws.CLOSE:
                return
            if opcode == ws.PING:
                self._send(ws.PONG, payload)
            elif opcode == ws.BINARY and self._writable:
                os.write(master, payload)
            elif opcode == ws.TEXT:
                self._control(json.loads(payload), master, tty)

    def _control(self, message: dict[str, object], master: int, tty: str) -> None:
        kind = message.get("type")
        if kind == "resize":
            _set_size(master, int(message.get("cols", 80)), int(message.get("rows", 24)))  # type: ignore[arg-type]
        elif kind == "writable":
            wanted = bool(message.get("value")) and self._writable_allowed
            self._manager.set_read_only(tty, not wanted)
            self._writable = wanted


__all__ = ["Viewer"]
