"""The per-host control plane + gateway for the docker backend (PRD 0070).

Runs the orchestrator (control plane) and the gateway (data plane) as **two
separate containers**, split now that #469 got the DB off the data plane:

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
from .orchestrator import (
    DockerOrchestrator,
    ORCHESTRATOR_IMAGE,
    ORCHESTRATOR_LABEL,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_NETWORK,
)
from ... import resources
from ... import release_manifest
from ...gateway import (
    GATEWAY_IMAGE,
    GATEWAY_NAME,
    GATEWAY_NETWORK,
)
from ..infra_service import InfraService
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
)

# Back-compat aliases: the split retired the combined `bot-bottle-infra`
# container, but the fixed-name constants some callers/tests import still map to
# the pair's public identity.
INFRA_NAME = GATEWAY_NAME  # the container agents attribute against is the gateway


class DockerInfraService(InfraService):
    """Composes the per-host control plane + gateway as two containers.

    `orchestrator_name` / `gateway_name` let callers run independent pairs on
    one host without name collisions (e.g. isolated integration tests that
    can't share the production singletons)."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        control_network: str = ORCHESTRATOR_NETWORK,
        orchestrator_image: str = ORCHESTRATOR_IMAGE,
        gateway_image: str = GATEWAY_IMAGE,
        repo_root: Path | None = None,
        host_root: Path | None = None,
        root_mount_source: str | Path | None = None,
        gateway_ca_mount_source: str | Path | None = None,
        orchestrator_name: str = ORCHESTRATOR_NAME,
        orchestrator_label: str = ORCHESTRATOR_LABEL,
        gateway_name: str = GATEWAY_NAME,
    ) -> None:
        self.port = port
        self.network = network
        self.control_network = control_network
        self.orchestrator_image = orchestrator_image
        self.gateway_image = gateway_image
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
        resolved_orchestrator, orchestrator_local = release_manifest.oci_image(
            "orchestrator", orchestrator_image)
        resolved_gateway, gateway_local = release_manifest.oci_image(
            "gateway", gateway_image)
        self.orchestrator_image = resolved_orchestrator
        self.gateway_image = resolved_gateway
        self._orchestrator_local = orchestrator_local
        self._gateway_local = gateway_local

    def orchestrator(self) -> DockerOrchestrator:
        """The control-plane service. Cheap to reconstruct — `ensure_built`
        builds its image, `ensure_running` brings it up, and the launch flow
        reads its `url()` / `gateway_url()` / `mint_gateway_token()` off it."""
        return DockerOrchestrator(
            self.orchestrator_image,
            name=self._orchestrator_name,
            label=self._orchestrator_label,
            port=self.port,
            control_network=self.control_network,
            repo_root=self._repo_root,
            host_root=self._host_root,
            root_mount_source=self._root_mount_source,
            dockerfile=(
                "Dockerfile.orchestrator" if self._orchestrator_local else None),
        )

    def gateway(self) -> DockerGateway:
        """The data-plane gateway, dual-homed on the agent network + the control
        network, resolving policy against the orchestrator by name. Cheap to
        reconstruct — the launch flow reads its `address()` / provisioning
        transport off it, and `ensure_running` connects it to the control
        plane."""
        return DockerGateway(
            self.gateway_image,
            name=self._gateway_name,
            network=self.network,
            control_network=self.control_network,
            build_context=self._repo_root,
            ca_mount_source=self._gateway_ca_mount_source,
            dockerfile="Dockerfile.gateway" if self._gateway_local else None,
        )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Ensure the orchestrator + gateway containers are up; return the host
        control-plane URL. Idempotent — a healthy orchestrator on current source
        (and a current-image gateway) is left untouched. Raises
        `OrchestratorStartError` on control-plane startup timeout."""
        orchestrator = self.orchestrator()
        gateway = self.gateway()
        # Build both images (cache-aware; a no-op when nothing changed) before
        # bringing either plane up.
        orchestrator.ensure_available()
        gateway.ensure_available()

        orchestrator.ensure_running(startup_timeout=startup_timeout)

        # Bring up (or refresh) the gateway once the control plane it resolves
        # against is healthy. The orchestrator (which holds the signing key)
        # mints the role-scoped `gateway` JWT here and hands it to the gateway,
        # which never sees the key (#469). `connect_to_orchestrator` is
        # idempotent.
        gateway.connect_to_orchestrator(
            orchestrator.gateway_url(), orchestrator.mint_gateway_token(),
        )
        return orchestrator.url()

    def stop(self) -> None:
        """Remove both containers (idempotent)."""
        run_docker(["docker", "rm", "--force", self._gateway_name])
        run_docker(["docker", "rm", "--force", self._orchestrator_name])


__all__ = [
    "DockerInfraService",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
]
