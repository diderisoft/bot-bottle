"""Uvicorn transport for the FastAPI orchestrator control plane."""

from __future__ import annotations

import os
import socket
import threading

import uvicorn

from ..trust_domain import CONTROL_PLANE
from .api import create_app
from ..contract.http import MAX_BODY_BYTES, ORCHESTRATOR_AUTH_HEADER
from .service import OrchestratorCore

MAX_REQUESTS = 32
KEEP_ALIVE_TIMEOUT_SECONDS = 10


class OrchestratorServer:
    """Small lifecycle wrapper around Uvicorn with an eagerly bound socket."""

    def __init__(self, config: uvicorn.Config) -> None:
        self._server = uvicorn.Server(config)
        self._stopped = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((config.host, config.port))
        self._socket.listen(config.backlog)
        self.server_address = self._socket.getsockname()

    def run(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._stopped.set()

    def serve_forever(self) -> None:
        self.run()

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._stopped.wait(timeout=5)

    def server_close(self) -> None:
        self._socket.close()


def make_server(
    orchestrator: OrchestratorCore,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    signing_key: str | None = None,
) -> OrchestratorServer:
    """Build a bounded Uvicorn server around the orchestrator application."""
    key = CONTROL_PLANE.key_from_env() if signing_key is None else signing_key
    app = create_app(orchestrator, signing_key=key)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=bool(os.environ.get("BOT_BOTTLE_ORCHESTRATOR_DEBUG")),
        log_level="info",
        limit_concurrency=MAX_REQUESTS,
        timeout_keep_alive=KEEP_ALIVE_TIMEOUT_SECONDS,
        server_header=False,
    )
    return OrchestratorServer(config)


__all__ = [
    "KEEP_ALIVE_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_REQUESTS",
    "ORCHESTRATOR_AUTH_HEADER",
    "OrchestratorServer",
    "create_app",
    "make_server",
]
