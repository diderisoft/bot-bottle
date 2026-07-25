"""The `GatewayTransport` for the Apple gateway container (PRD 0070).

How the launcher stages files + runs commands in the running gateway container:
the `container` CLI's exec/cp equivalents. The backend-neutral provisioning
logic that drives it lives in `backend.gateway_provision`; Docker uses
`docker exec`/`docker cp` and Firecracker uses SSH.
"""

from __future__ import annotations

from ...gateway import GatewayProvisionError
from . import util as container_mod
from .gateway import GATEWAY_NAME


class MacosGatewayTransport:
    """`GatewayTransport` for the gateway daemons in the Apple gateway container."""

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


__all__ = ["MacosGatewayTransport"]
