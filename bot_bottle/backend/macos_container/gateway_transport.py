"""`GatewayTransport` for the Apple gateway container: `container` exec and cp.
"""

from __future__ import annotations

from pathlib import Path

from ...control.gateway import GatewayProvisionError
from . import util as container_mod
from .gateway import GATEWAY_NAME

TMPFS = "/dev/shm/"


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
        if dest.startswith(TMPFS):
            # `container cp` writes beneath a tmpfs mount, so the file never
            # shows up in it (exit 0 all the same): stream it in instead.
            # `-i` leaks a pipe per exec (apple/container#2439); this runs per
            # launch, grant and token renewal, not periodically.
            result = container_mod.run_container_argv(
                ["container", "exec", "-i", self.gateway, "sh", "-c", 'cat > "$1"', "sh", dest],
                stdin=Path(src).read_text(encoding="utf-8"))
        else:
            result = container_mod.run_container_argv(
                ["container", "cp", src, f"{self.gateway}:{dest}"]
            )
        if result.returncode != 0:
            raise GatewayProvisionError(
                f"gateway cp {src} -> {dest} failed: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )


__all__ = ["MacosGatewayTransport"]
