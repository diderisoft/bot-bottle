"""Dependency-free constants shared by orchestrator HTTP clients and server."""

ORCHESTRATOR_AUTH_HEADER = "x-bot-bottle-orchestrator-auth"
MAX_BODY_BYTES = 1 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10.0

__all__ = [
    "MAX_BODY_BYTES",
    "ORCHESTRATOR_AUTH_HEADER",
    "REQUEST_BODY_TIMEOUT_SECONDS",
]
