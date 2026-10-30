"""The macOS infra pair: two Apple containers. Only the orchestrator opens
`bot-bottle.db`, since two guests sharing it over virtiofs would race
incoherent `fcntl` locks.

- `bot-bottle-mac-orchestrator`: on the host-only control network only, with
  a container-only DB volume and the signing key. The CLI and the gateway
  reach it at its control-network address.
- `bot-bottle-mac-infra` (the gateway): on the NAT egress, host-only agent
  and control networks. Holds the CA and the `gateway` token.

Agents are on the agent network only, so they have no route to the control
plane.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ... import resources
from ...contract.hostctl import Settings
from ...control.lifecycle import (
    DEFAULT_PORT,
    OrchestratorStartError,
)
from ...infra_image import BUILD_WITH_CONTAINER
from ..infra_service import InfraService
from . import util as container_mod
from .gateway import (
    CONTROL_NETWORK,
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_EGRESS_NETWORK,
    GATEWAY_NAME,
    GATEWAY_NETWORK,
    MacosGateway,
    ensure_networks,
)
from .orchestrator import (
    ORCHESTRATOR_DB_VOLUME,
    ORCHESTRATOR_NAME,
    MacosOrchestrator,
    probe_orchestrator_url,
)

if TYPE_CHECKING:
    from .gateway_attach import MacosGatewayAttacher

# Alias of the gateway container, for callers that import it.
INFRA_NAME = GATEWAY_NAME


class MacosInfraService(InfraService):
    name = "macos-container"
    """Composes the per-host orchestrator + gateway containers. Callers use
    `ensure_running()` (returns the control-plane URL), the `orchestrator()` /
    `gateway()` accessors, and `ca_cert_pem()`."""
    infra_build_note = BUILD_WITH_CONTAINER

    def __init__(
        self,
        settings: Settings,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        control_network: str = CONTROL_NETWORK,
        gateway_image: str | None = None,
        orchestrator_image: str | None = None,
        repo_root: Path | None = None,
        orchestrator_name: str = ORCHESTRATOR_NAME,
        gateway_name: str = INFRA_NAME,
        db_volume: str = ORCHESTRATOR_DB_VOLUME,
    ) -> None:
        self.settings = settings
        self.port = port
        self.network = network
        self.egress_network = egress_network
        self.control_network = control_network
        self.gateway_image = gateway_image or settings.infra.gateway_image
        self.orchestrator_image = orchestrator_image or settings.infra.orchestrator_image
        # Build context: the checkout root, or a copy staged from the wheel.
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        self._orchestrator_name = orchestrator_name
        self._gateway_name = gateway_name
        self._db_volume = db_volume

    def orchestrator(self) -> MacosOrchestrator:
        """The control-plane service. Cheap to reconstruct."""
        return MacosOrchestrator(
            self.orchestrator_image,
            settings=self.settings,
            name=self._orchestrator_name,
            port=self.port,
            control_network=self.control_network,
            repo_root=self._repo_root,
            db_volume=self._db_volume,
        )

    def gateway(self) -> MacosGateway:
        """The gateway service. Cheap to reconstruct."""
        return MacosGateway(
            self.gateway_image,
            settings=self.settings,
            name=self._gateway_name,
            network=self.network,
            egress_network=self.egress_network,
            control_network=self.control_network,
            repo_root=self._repo_root,
        )

    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Ensure both containers are up and return the control-plane URL. A
        healthy, current orchestrator is left alone. Raises
        `OrchestratorStartError` on timeout."""
        # Networks first.
        ensure_networks(self.network, self.egress_network, self.control_network)

        orchestrator = self.orchestrator()
        gateway = self.gateway()
        orchestrator.ensure_built()
        gateway.ensure_built()

        orchestrator_restarted = orchestrator.ensure_running(startup_timeout=startup_timeout)

        # The gateway needs the orchestrator's address; the orchestrator mints
        # its token.
        url = orchestrator.url()
        gateway_restarted = gateway.connect_to_orchestrator(
            url, orchestrator.mint_gateway_token())
        return url, orchestrator_restarted or gateway_restarted

    def gateway_attacher(self) -> "MacosGatewayAttacher":
        # Local import to avoid a cycle with `enumerate`.
        from .gateway_attach import MacosGatewayAttacher
        return MacosGatewayAttacher(self.network)

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's CA PEM."""
        return self.gateway().ca_cert_pem(timeout=timeout)

    def stop(self) -> None:
        """Remove both containers. Idempotent; the DB volume persists."""
        container_mod.force_remove_container(self._gateway_name)
        container_mod.force_remove_container(self._orchestrator_name)


__all__ = [
    "MacosInfraService",
    "OrchestratorStartError",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
    "probe_orchestrator_url",
]
