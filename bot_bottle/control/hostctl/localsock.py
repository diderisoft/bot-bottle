"""The host controller's local Unix socket, for the processes tmux runs.

A session's launcher fetches its command and environment (`spec`, handed out
once); the `pane-died` hook reports the agent's exit (`died`). One JSON line
in, one JSON line out. The socket lives in the 0700 state dir and is 0600.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

from ...lib import log
from .sessions import ExitStatus, SessionManager


def serve(path: Path, manager: SessionManager) -> socket.socket:
    """Bind `path` and answer requests on a daemon thread; returns the socket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(16)

    def loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn, manager), daemon=True).start()

    threading.Thread(target=loop, daemon=True, name="hostctl-localsock").start()
    return server


def _handle(conn: socket.socket, manager: SessionManager) -> None:
    with conn, conn.makefile("rw", encoding="utf-8") as stream:
        try:
            body = json.loads(stream.readline())
            reply = _answer(body, manager)
        except (ValueError, KeyError, TypeError) as e:
            reply = {"error": f"bad request: {e}"}
        stream.write(json.dumps(reply) + "\n")
        stream.flush()


def _answer(body: dict[str, object], manager: SessionManager) -> dict[str, object]:
    op = body["op"]
    session_id = str(body["session"])
    if op == "spec":
        spec = manager.spec(session_id)
        if spec is None:
            return {"error": f"no pending command for session {session_id}"}
        return {"argv": spec[0], "env": spec[1]}
    if op == "died":
        status = str(body.get("status", ""))
        exit_status = ExitStatus(int(status) if status.lstrip("-").isdigit() else 0,
                                 str(body.get("signal", "")))
        # Finishing tears the bottle down; don't hold tmux's hook open for it.
        threading.Thread(target=manager.died, args=(session_id, exit_status), daemon=True).start()
        return {"ok": True}
    log.warn(f"hostctl local socket: unknown op {op!r}")
    return {"error": f"unknown op {op!r}"}


def request(path: Path, body: dict[str, object]) -> dict[str, object]:
    """One request to the daemon's local socket (used by launch and notify)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(path))
        with conn.makefile("rw", encoding="utf-8") as stream:
            stream.write(json.dumps(body) + "\n")
            stream.flush()
            return json.loads(stream.readline())


__all__ = ["request", "serve"]
