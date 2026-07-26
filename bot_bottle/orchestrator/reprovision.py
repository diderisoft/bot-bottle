"""Shared host-side join for backend-discovered bottle encryption keys."""

from __future__ import annotations

from ..log import debug
from .client import OrchestratorClient, OrchestratorClientError


def reprovision_bottles(
    client: OrchestratorClient,
    secrets_by_source_ip: dict[str, str],
) -> int:
    """Restore tokens for registered bottles whose backend exposes a key.

    Backends own discovery because containers and microVMs have different
    enumeration primitives. This helper owns the shared registry join and
    intentionally tolerates one bad/missing key without blocking a launch.
    """
    restored = 0
    for bottle in client.list_bottles():
        bottle_id = bottle.get("bottle_id")
        source_ip = bottle.get("source_ip")
        if not isinstance(bottle_id, str) or not isinstance(source_ip, str):
            continue
        secret = secrets_by_source_ip.get(source_ip, "").strip()
        if not secret:
            continue
        try:
            if client.reprovision_gateway(bottle_id, secret):
                restored += 1
        except OrchestratorClientError as exc:
            debug(
                "gateway secret reprovision failed; continuing with other bottles",
                context={
                    "bottle_id": bottle_id,
                    "error_type": type(exc).__name__,
                },
            )
            continue
    return restored


__all__ = ["reprovision_bottles"]
