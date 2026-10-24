"""`GatewayTransport` for the Apple gateway container: `container` exec and cp.
"""

from __future__ import annotations

from ...control.gateway import GatewayProvisionError
from . import util as container_mod
from .gateway import GATEWAY_NAME


class MacosGatewayTransport:

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
