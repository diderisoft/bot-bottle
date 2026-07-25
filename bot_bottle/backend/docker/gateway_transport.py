"""The `GatewayTransport` for the docker gateway container (PRD 0070).

How the launcher stages files + runs commands in the running gateway container:
`docker exec` / `docker cp` over the docker socket. The backend-neutral
provisioning logic that drives it lives in `backend.gateway_provision`.
"""

from __future__ import annotations

from .util import run_docker
from ...gateway import GatewayProvisionError


class DockerGatewayTransport:
    """`GatewayTransport` for the docker gateway container (exec/cp)."""

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
