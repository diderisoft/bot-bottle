"""Uvicorn transport for the FastAPI orchestrator control plane."""

from __future__ import annotations

import os
import socket
import threading

import uvicorn

from ..orchestrator_auth import ROLE_CLI, mint
from ..trust_domain import CONTROL_PLANE
from .api import create_app
from .http_contract import MAX_BODY_BYTES, ORCHESTRATOR_AUTH_HEADER
from .service import OrchestratorCore

MAX_REQUESTS = 32
KEEP_ALIVE_TIMEOUT_SECONDS = 10


def dispatch(
    orchestrator: OrchestratorCore,
    method: str,
    path: str,
    body: bytes,
    *,
    role: str | None = ROLE_CLI,
) -> tuple[int, dict[str, object]]:
    """Socket-free compatibility adapter for route unit tests.

    Production requests always enter through the FastAPI ASGI application.
    """
    from fastapi.testclient import TestClient

    key = "in-process-dispatch-key"
    headers: dict[str, str] = {"content-type": "application/json"}
    if role is not None:
        headers[ORCHESTRATOR_AUTH_HEADER] = mint(role, key)
    response = TestClient(create_app(orchestrator, signing_key=key)).request(
        method, path, content=body, headers=headers,
    )
    payload = response.json()
    if response.status_code == 422:
        detail = payload.get("detail", []) if isinstance(payload, dict) else []
        field = ""
        if isinstance(detail, list) and detail and isinstance(detail[0], dict):
            location = detail[0].get("loc", ())
            if isinstance(location, (list, tuple)) and len(location) > 1:
                field = str(location[1])
        suffix = f": {field}" if field else ""
        return 400, {"error": f"invalid request body{suffix}"}
    if isinstance(payload, dict) and "detail" in payload and "error" not in payload:
        payload = {"error": payload["detail"]}
    return response.status_code, payload


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
    "dispatch",
    "make_server",
]
