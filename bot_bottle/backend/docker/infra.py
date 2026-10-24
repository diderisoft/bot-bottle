"""The docker infra pair: two containers.

- `bot-bottle-orchestrator`: on the `--internal` control network, plus a
  loopback publish for the CLI. Sole opener of `bot-bottle.db`; holds the key.
- `bot-bottle-gateway`: on the agent network and the control network, so it
  reaches the orchestrator by name while agents can't reach it at all. Holds
  the `gateway` token and the CA.
"""

from __future__ import annotations

from pathlib import Path

from .util import run_docker
from .gateway import DockerGateway
from .gateway_attach import DockerGatewayAttacher
from .orchestrator import (
    DockerOrchestrator,
    ORCHESTRATOR_LABEL,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_NETWORK,
)
from ... import resources
from ...contract.hostctl import Settings
from ...control.gateway import (
    GATEWAY_NAME,
    GATEWAY_NETWORK,
)
from ..infra_service import InfraService
from ...control.lifecycle import (
    DEFAULT_PORT,
)

# Aliases kept for callers that import the old combined-container names.
INFRA_NAME = GATEWAY_NAME  # agents attribute against the gateway


class DockerInfraService(InfraService):
    name = "docker"
    """Composes the per-host control plane + gateway as two containers.

    `orchestrator_name` / `gateway_name` let callers run independent pairs on
    one host without name collisions (e.g. isolated integration tests that
    can't share the production singletons)."""

    def __init__(
        self,
        settings: Settings,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        control_network: str = ORCHESTRATOR_NETWORK,
        orchestrator_image: str | None = None,
        gateway_image: str | None = None,
        repo_root: Path | None = None,
        host_root: Path | None = None,
        root_mount_source: str | Path | None = None,
        gateway_ca_mount_source: str | Path | None = None,
        orchestrator_name: str = ORCHESTRATOR_NAME,
        orchestrator_label: str = ORCHESTRATOR_LABEL,
        gateway_name: str = GATEWAY_NAME,
    ) -> None:
        self.settings = settings
        self.port = port
        self.network = network
        self.control_network = control_network
        self.orchestrator_image = orchestrator_image or settings.infra.orchestrator_image
        self.gateway_image = gateway_image or settings.infra.gateway_image
        # Build context: the checkout root, or a copy staged from the wheel.
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        if host_root is not None and root_mount_source is not None:
            raise ValueError("pass host_root or root_mount_source, not both")
        self._host_root = host_root
        self._root_mount_source = root_mount_source
        self._gateway_ca_mount_source = gateway_ca_mount_source
        self._orchestrator_name = orchestrator_name
        self._orchestrator_label = orchestrator_label
        self._gateway_name = gateway_name

    def orchestrator(self) -> DockerOrchestrator:
        """The control-plane service. Cheap to reconstruct."""
        return DockerOrchestrator(
            self.orchestrator_image,
            settings=self.settings,
            name=self._orchestrator_name,
            label=self._orchestrator_label,
            port=self.port,
            control_network=self.control_network,
            repo_root=self._repo_root,
            host_root=self._host_root,
            root_mount_source=self._root_mount_source,
        )

    def gateway(self) -> DockerGateway:
        """The gateway service, on the agent and control networks. Cheap to
        reconstruct."""
        return DockerGateway(
            self.gateway_image,
            settings=self.settings,
            name=self._gateway_name,
            network=self.network,
            control_network=self.control_network,
            build_context=self._repo_root,
            ca_mount_source=self._gateway_ca_mount_source,
        )

    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Ensure both containers are up and return the host control-plane URL.
        A healthy, current pair is left alone. Raises `OrchestratorStartError`
        on timeout."""
        orchestrator = self.orchestrator()
        gateway = self.gateway()
        # Build both images (cache-aware) first.
        orchestrator.ensure_built()
        gateway.ensure_built()

        orchestrator_restarted = orchestrator.ensure_running(startup_timeout=startup_timeout)

        # Once the orchestrator is healthy it mints the gateway's token.
        # Reports whether the gateway (re)started.
        gateway_restarted = gateway.connect_to_orchestrator(
            orchestrator.gateway_url(), orchestrator.mint_gateway_token(),
        )
        return orchestrator.url(), orchestrator_restarted or gateway_restarted

    def gateway_attacher(self) -> DockerGatewayAttacher:
        return DockerGatewayAttacher(self.network)

    def stop(self) -> None:
        """Remove both containers. Idempotent."""
        run_docker(["docker", "rm", "--force", self._gateway_name])
        run_docker(["docker", "rm", "--force", self._orchestrator_name])


__all__ = [
    "DockerInfraService",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
]
