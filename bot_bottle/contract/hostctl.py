"""The host controller's wire contract (PRD 0092): routes and response shapes
shared by the daemon and its clients."""

from __future__ import annotations

SERVICE_NAME = "bot-bottle-hostctl"

# Port the daemon binds; unset means an ephemeral port, published in its state file.
PORT_ENV = "BOT_BOTTLE_HOSTCTL_PORT"

STATUS_PATH = "/status"


def status_body(*, build_id: str, pid: int) -> dict[str, object]:
    """`GET /status` response: which code the daemon runs and its pid."""
    return {"service": SERVICE_NAME, "build_id": build_id, "pid": pid}


__all__ = ["PORT_ENV", "SERVICE_NAME", "STATUS_PATH", "status_body"]
