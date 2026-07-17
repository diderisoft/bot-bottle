"""`GatewayTransport` for the Apple gateway container (PRD 0070).

The provisioning *logic* (per-bottle creds dirs, namespaced repo init) is
backend-neutral and lives in `backend.docker.gateway_provision`; this is only
the transport — how files and commands reach the running gateway. Docker uses
`docker exec`/`docker cp` and Firecracker uses SSH; Apple uses the `container`
CLI's equivalents.
"""

from __future__ import annotations

from ..docker.gateway_provision import GatewayProvisionError
from . import util as container_mod
from .gateway import GATEWAY_NAME


class AppleGatewayTransport:
    """`GatewayTransport` for the gateway as an Apple container."""

    def __init__(self, gateway: str = GATEWAY_NAME) -> None:
        self.gateway = gateway

    def exec(self, argv: list[str]) -> None:
        result = container_mod.run_container_argv(
            ["container", "exec", self.gateway, *argv]
        )
        if result.returncode != 0:
            raise GatewayProvisionError(
                f"gateway exec {argv!r} failed: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def cp_into(self, src: str, dest: str) -> None:
        result = container_mod.run_container_argv(
            ["container", "cp", src, f"{self.gateway}:{dest}"]
        )
        if result.returncode != 0:
            raise GatewayProvisionError(
                f"gateway cp {src} -> {dest} failed: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )


__all__ = ["AppleGatewayTransport", "GatewayProvisionError"]
