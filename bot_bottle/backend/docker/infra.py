"""The per-host control plane + gateway for the docker backend.

Runs the orchestrator (control plane) and the gateway (data plane) as **two
separate containers**:

  * `bot-bottle-orchestrator` — the lean control-plane container
    (`DockerOrchestrator`). Joins the `bot-bottle-orchestrator` **control
    network** (`--internal`) only, plus a host-loopback publish for the CLI.
    Sole opener of `bot-bottle.db`; holds the signing key.
  * `bot-bottle-gateway` — the data-plane container (`DockerGateway`).
    **Dual-homed** on the agent-facing `bot-bottle-gateway` network *and* the
    control network, so it reaches the orchestrator by name over docker DNS
    (`http://bot-bottle-orchestrator:8099`) while agents — which are never on
    the control network — cannot reach the control plane at all (the L3 block
    Firecracker's nft already has). Holds the `gateway` JWT + the mitmproxy CA.

`DockerInfraService` composes the two services (`orchestrator()` + `gateway()`)
and brings them up as an idempotent per-host pair. Callers use `ensure_running()`
(returns the host control-plane URL) + `gateway_name` (the container the launch
flow attributes agents against).
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

# Back-compat aliases: the split retired the combined `bot-bottle-infra`
# container, but the fixed-name constants some callers/tests import still map to
# the pair's public identity.
INFRA_NAME = GATEWAY_NAME  # the container agents attribute against is the gateway


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
        # Build context: the repo root in a checkout, a staged copy from the
        # installed wheel otherwise (bot_bottle.resources).
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
        """The control-plane service. Cheap to reconstruct — `ensure_built`
        builds its image, `ensure_running` brings it up, and the launch flow
        reads its `url()` / `gateway_url()` / `mint_gateway_token()` off it."""
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
        """The data-plane gateway, dual-homed on the agent network + the control
        network, resolving policy against the orchestrator by name. Cheap to
        reconstruct — the launch flow reads its `address()` / provisioning
        transport off it, and `ensure_running` connects it to the control
        plane."""
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
        """Ensure the orchestrator + gateway containers are up; return the host
        control-plane URL. Idempotent — a healthy orchestrator on current source
        (and a current-image gateway) is left untouched. Raises
        `OrchestratorStartError` on control-plane startup timeout."""
        orchestrator = self.orchestrator()
        gateway = self.gateway()
        # Build both images (cache-aware; a no-op when nothing changed) before
        # bringing either plane up.
        orchestrator.ensure_built()
        gateway.ensure_built()

        orchestrator_restarted = orchestrator.ensure_running(startup_timeout=startup_timeout)

        # Bring up (or refresh) the gateway once the control plane it resolves
        # against is healthy. The orchestrator (which holds the signing key)
        # mints the role-scoped `gateway` JWT here and hands it to the gateway,
        # which never sees the key. `connect_to_orchestrator` is
        # idempotent and reports whether it (re)started the gateway.
        gateway_restarted = gateway.connect_to_orchestrator(
            orchestrator.gateway_url(), orchestrator.mint_gateway_token(),
        )
        return orchestrator.url(), orchestrator_restarted or gateway_restarted

    def gateway_attacher(self) -> DockerGatewayAttacher:
        return DockerGatewayAttacher(self.network)

    def stop(self) -> None:
        """Remove both containers (idempotent)."""
        run_docker(["docker", "rm", "--force", self._gateway_name])
        run_docker(["docker", "rm", "--force", self._orchestrator_name])


__all__ = [
    "DockerInfraService",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
]
