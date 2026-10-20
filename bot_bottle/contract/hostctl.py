"""The host controller's wire contract (PRD 0092): routes, response shapes and
its trust domain, shared by the daemon and its clients."""

from __future__ import annotations

from .auth import ROLE_CLI, ROLE_GATEWAY, TrustDomain

SERVICE_NAME = "bot-bottle-hostctl"

# Port the daemon binds; unset means an ephemeral port, published in its state file.
PORT_ENV = "BOT_BOTTLE_HOSTCTL_PORT"

STATUS_PATH = "/status"
# GET /jobs, /jobs/{id}, /jobs/{id}/events (newline-delimited JSON, streamed).
JOBS_PATH = "/jobs"

# Callers authenticate with `Authorization: Bearer <token>`.
AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"

ROLE_ORCHESTRATOR = "orchestrator"

# The host controller's own key, which the orchestrator never holds: it gets an
# `orchestrator` token instead, so it can't mint `cli` tokens and restart itself.
HOST_CONTROL = TrustDomain(
    name="host-control",
    key_filename="hostctl-token",
    roles=frozenset({ROLE_CLI, ROLE_ORCHESTRATOR, ROLE_GATEWAY}),
    key_env="BOT_BOTTLE_HOSTCTL_KEY",
    token_env="BOT_BOTTLE_HOSTCTL_JWT",
)


def status_body(*, build_id: str, pid: int) -> dict[str, object]:
    """`GET /status` response: which code the daemon runs and its pid."""
    return {"service": SERVICE_NAME, "build_id": build_id, "pid": pid}


def bearer(token: str) -> dict[str, str]:
    """The request header carrying `token`."""
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


__all__ = [
    "AUTH_HEADER", "AUTH_SCHEME", "HOST_CONTROL", "JOBS_PATH", "PORT_ENV", "ROLE_ORCHESTRATOR",
    "SERVICE_NAME", "STATUS_PATH", "bearer", "status_body",
]
