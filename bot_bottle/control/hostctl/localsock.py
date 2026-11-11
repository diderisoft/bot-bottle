"""The host controller's control socket, at a fixed path
(`runner.control_socket_path`): the CLI asks for its `status` (pid, HTTP port
and build id) to find it, the tmux launcher fetches its `spec` (once) and the
`pane-died` hook reports `died`. One JSON line each way. 0600, in the 0700
state dir.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

from ...lib import log
from .sessions import ExitStatus, SessionManager


# The daemon's `status` reply, or None until its HTTP server is up.
Status = Callable[[], "dict[str, object] | None"]


def serve(path: Path, manager: SessionManager,
          status_of: Status = lambda: None) -> socket.socket:
    """Bind `path` and answer requests on a daemon thread; returns the socket.
    A socket already at `path` is replaced: the caller holds the daemon's
    lifetime lock (`runner.hold_daemon_lock`), so it's a dead daemon's."""
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
            threading.Thread(target=_handle, args=(conn, manager, status_of),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True, name="hostctl-localsock").start()
    return server


def _handle(conn: socket.socket, manager: SessionManager, status_of: Status) -> None:
    with conn, conn.makefile("rw", encoding="utf-8") as stream:
        try:
            body = json.loads(stream.readline())
            reply = _answer(body, manager, status_of)
        except (ValueError, KeyError, TypeError) as e:
            reply = {"error": f"bad request: {e}"}
        stream.write(json.dumps(reply) + "\n")
        stream.flush()


def _answer(body: dict[str, object], manager: SessionManager,
            status_of: Status) -> dict[str, object]:
    op = body["op"]
    if op == "status":
        return status_of() or {"error": "starting"}
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
        # Finishing tears the bottle down; don't hold tmux's hook open.
        threading.Thread(target=manager.died, args=(session_id, exit_status), daemon=True).start()
        return {"ok": True}
    log.warn(f"hostctl local socket: unknown op {op!r}")
    return {"error": f"unknown op {op!r}"}


def request(path: Path, body: dict[str, object],
            timeout: float | None = None) -> dict[str, object]:
    """One request to the daemon's socket."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        with conn.makefile("rw", encoding="utf-8") as stream:
            stream.write(json.dumps(body) + "\n")
            stream.flush()
            return json.loads(stream.readline())


__all__ = ["request", "serve"]
