"""`GatewayTransport` for the docker gateway: `docker exec` and `docker cp`."""

from __future__ import annotations

from .util import run_docker
from ...control.gateway import GatewayProvisionError


class DockerGatewayTransport:

    def __init__(self, gateway: str) -> None:
        self.gateway = gateway

    def exec(self, argv: list[str]) -> None:
        proc = run_docker(["docker", "exec", self.gateway, *argv])
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"gateway exec {argv!r} failed: {proc.stderr.strip()}"
            )

    def cp_into(self, src: str, dest: str) -> None:
        proc = run_docker(["docker", "cp", src, f"{self.gateway}:{dest}"])
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"gateway cp {src} -> {dest} failed: {proc.stderr.strip()}"
            )


__all__ = ["DockerGatewayTransport"]
