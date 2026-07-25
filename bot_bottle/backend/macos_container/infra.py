"""The per-host control plane + gateway for the macOS backend (PRD 0070).

Two Apple containers — the orchestrator (control plane) and the gateway (data
plane) — split now that #469 got the DB off the data plane. The single-container
model existed only because two Apple-Container guests writing one `bot-bottle.db`
over virtiofs would race incoherent `fcntl` locks; with the data plane no longer
opening the DB at all, only the orchestrator does, so the split is safe.

  * `bot-bottle-mac-orchestrator` — the lean control plane (`MacosOrchestrator`).
    Joins the host-only **control network** (`bot-bottle-mac-control`) only. Sole
    opener of the container-only DB volume; holds the signing key. The host CLI
    reaches it at its control-network address; the gateway reaches it there too.
  * `bot-bottle-mac-infra` — the gateway data plane (`MacosGateway`). Triple-homed:
    the NAT egress network (route out), the host-only agent network (agents + CLI
    reach the gateway), and the control network (reach the orchestrator by IP —
    Apple has no container DNS). Holds the mitmproxy CA + the `gateway` JWT.

`MacosInfraService` composes the two services and brings them up as an idempotent
per-host pair. Agents sit on the agent network only, never the control network,
so they have no route to the control plane (the L3 block, not just the JWT).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    OrchestratorStartError,
)
from . import util as container_mod
from .gateway import (
    CONTROL_NETWORK,
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_EGRESS_NETWORK,
    GATEWAY_IMAGE,
    GATEWAY_NAME,
    GATEWAY_NETWORK,
    GatewayError,
    MacosGateway,
    ensure_networks,
)
from .orchestrator import (
    ORCHESTRATOR_DB_VOLUME,
    ORCHESTRATOR_IMAGE,
    ORCHESTRATOR_NAME,
    MacosOrchestrator,
    probe_orchestrator_url,
)

# `INFRA_NAME` is kept — now aliasing the gateway container — for callers that
# still import it (probe / reprovision attribute against the gateway).
INFRA_NAME = GATEWAY_NAME
# Back-compat alias: the DB volume moved to the orchestrator module.
INFRA_DB_VOLUME = ORCHESTRATOR_DB_VOLUME

_REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class InfraEndpoint:
    """How to reach the running pair. `orchestrator_url` is the orchestrator's
    control-network address (host CLI + registration); `gateway_ip` is the
    gateway's agent-network address (proxy / git-http / MCP target)."""

    orchestrator_url: str
    gateway_ip: str


class MacosInfraService:
    """Composes the per-host orchestrator + gateway containers. Callers use
    `ensure_running()` (returns the endpoint) and `ca_cert_pem()`."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        control_network: str = CONTROL_NETWORK,
        gateway_image: str = GATEWAY_IMAGE,
        orchestrator_image: str = ORCHESTRATOR_IMAGE,
        repo_root: Path = _REPO_ROOT,
        orchestrator_name: str = ORCHESTRATOR_NAME,
        gateway_name: str = INFRA_NAME,
        db_volume: str = ORCHESTRATOR_DB_VOLUME,
    ) -> None:
        self.port = port
        self.network = network
        self.egress_network = egress_network
        self.control_network = control_network
        self.gateway_image = gateway_image
        self.orchestrator_image = orchestrator_image
        self._repo_root = repo_root
        self._orchestrator_name = orchestrator_name
        self._gateway_name = gateway_name
        self._db_volume = db_volume

    @property
    def gateway_name(self) -> str:
        return self._gateway_name

    def orchestrator(self) -> MacosOrchestrator:
        """The control-plane service on the host-only control network. Cheap to
        reconstruct — the launch flow reads its `url()` / `gateway_url()` /
        `mint_gateway_token()` off it."""
        return MacosOrchestrator(
            self.orchestrator_image,
            name=self._orchestrator_name,
            port=self.port,
            control_network=self.control_network,
            repo_root=self._repo_root,
            db_volume=self._db_volume,
        )

    def gateway(self) -> MacosGateway:
        """The data-plane gateway service, triple-homed on the egress + agent +
        control networks. Cheap to reconstruct — the launch flow reads its
        `address()` / CA / provisioning transport off it, and `ensure_running`
        connects it to the control plane."""
        return MacosGateway(
            self.gateway_image,
            name=self._gateway_name,
            network=self.network,
            egress_network=self.egress_network,
            control_network=self.control_network,
            repo_root=self._repo_root,
        )

    def _resolve_gateway_ip(self) -> str:
        """The gateway's agent-network address, or "" while it has none."""
        return container_mod.try_container_ipv4_on_network(
            self._gateway_name, self.network)

    def is_healthy(self) -> bool:
        return self.orchestrator().is_healthy()

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> InfraEndpoint:
        """Ensure the orchestrator + gateway containers are up; return how to
        reach them. Idempotent per-host singleton — a healthy orchestrator on
        current source is left untouched. Raises `OrchestratorStartError` on
        control-plane startup timeout."""
        # The networks (host-only agent + NAT egress + host-only control) are a
        # shared concern — create them before either plane comes up.
        ensure_networks(self.network, self.egress_network, self.control_network)

        orchestrator = self.orchestrator()
        gateway = self.gateway()
        orchestrator.ensure_built()
        gateway.ensure_built()

        orchestrator.ensure_running(startup_timeout=startup_timeout)

        # (Re)ensure the gateway once the control plane it resolves against is
        # healthy — it needs the orchestrator's control-network address. The
        # orchestrator (which holds the signing key) mints the role-scoped
        # `gateway` JWT and hands it to the gateway, which never sees the key
        # (#469).
        url = orchestrator.url()
        gateway.connect_to_orchestrator(url, orchestrator.mint_gateway_token())
        return InfraEndpoint(orchestrator_url=url, gateway_ip=self._resolve_gateway_ip())

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception — delegated to the gateway service (reads it out of the
        gateway container, polling until mitmproxy writes it)."""
        return self.gateway().ca_cert_pem(timeout=timeout)

    def stop(self) -> None:
        """Remove both containers (idempotent). The DB volume persists."""
        container_mod.force_remove_container(self._gateway_name)
        container_mod.force_remove_container(self._orchestrator_name)


__all__ = [
    "MacosInfraService",
    "InfraEndpoint",
    "OrchestratorStartError",
    "GatewayError",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
    "INFRA_DB_VOLUME",
    "probe_orchestrator_url",
]
