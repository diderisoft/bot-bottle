"""The per-host control plane + gateway for the macOS backend (PRD 0070).

Two Apple containers — the orchestrator (control plane) and the gateway (data
plane) — split now that #469 got the DB off the data plane. The single-container
model existed only because two Apple-Container guests writing one `bot-bottle.db`
over virtiofs would race incoherent `fcntl` locks; with the data plane no longer
opening the DB at all, only the orchestrator does, so the split is safe.

  * `bot-bottle-mac-orchestrator` — the lean control plane. Joins the host-only
    **control network** (`bot-bottle-mac-control`) only. Sole opener of the
    container-only DB volume; holds the signing key. The host CLI reaches it at
    its control-network address; the gateway reaches it there too.
  * `bot-bottle-mac-infra` — the gateway data plane. Triple-homed: the NAT
    egress network (route out), the host-only agent network (agents + CLI reach
    the gateway), and the control network (reach the orchestrator by IP — Apple
    has no container DNS). Holds the mitmproxy CA + the `gateway` JWT.

Agents sit on the agent network only, never the control network, so they have no
route to the control plane (the L3 block, not just the JWT).
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ... import log
from ...gateway import GATEWAY_CA_CERT, MITMPROXY_HOME
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    OrchestratorStartError,
    source_hash,
)
from ...orchestrator_auth import ROLE_GATEWAY, mint
from ...paths import (
    ORCHESTRATOR_AUTH_JWT_ENV,
    ORCHESTRATOR_TOKEN_ENV,
    HOST_DB_FILENAME,
    host_orchestrator_token,
    host_gateway_ca_dir,
)
from .. import util as backend_util
from . import util as container_mod
from .gateway import (
    CONTROL_NETWORK,
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_EGRESS_NETWORK,
    GATEWAY_IMAGE,
    GATEWAY_NETWORK,
    GatewayError,
    ORCHESTRATOR_IMAGE,
    ensure_networks,
)

# The orchestrator (control plane) container + the gateway (data plane)
# container. `INFRA_NAME` is kept — now the gateway container — for callers that
# still import it (probe / reprovision attribute against the gateway).
ORCHESTRATOR_NAME = "bot-bottle-mac-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-mac-orchestrator=1"
INFRA_NAME = "bot-bottle-mac-infra"  # the gateway container
INFRA_LABEL = "bot-bottle-mac-infra=1"
# Container-only volume holding bot-bottle.db, mounted ONLY into the
# orchestrator. One kernel writes it (never host-shared or cross-guest).
INFRA_DB_VOLUME = "bot-bottle-mac-db"

# BOT_BOTTLE_ROOT inside the orchestrator; host_db_path() resolves the DB to
# <root>/db/<filename>.
_DB_ROOT_IN_CONTAINER = "/var/lib/bot-bottle"
_DB_PATH_IN_CONTAINER = f"{_DB_ROOT_IN_CONTAINER}/db/{HOST_DB_FILENAME}"
_SRC_IN_CONTAINER = "/bot-bottle-src"

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEALTH_POLL_SECONDS = 0.25
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0

# The gateway subset the consolidated model runs (no per-bottle git:// daemon).
_GATEWAY_DAEMONS = "egress,git-http,supervise"


@dataclass(frozen=True)
class InfraEndpoint:
    """How to reach the running pair. `orchestrator_url` is the orchestrator's
    control-network address (host CLI + registration); `gateway_ip` is the
    gateway's agent-network address (proxy / git-http / MCP target)."""

    orchestrator_url: str
    gateway_ip: str


class MacosInfraService:
    """Manages the per-host orchestrator + gateway containers. Callers use
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
        db_volume: str = INFRA_DB_VOLUME,
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

    def _resolve_orchestrator_url(self) -> str:
        """The control-plane URL (orchestrator's control-network address), or ""
        while it has no address."""
        ip = container_mod.try_container_ipv4_on_network(
            self._orchestrator_name, self.control_network)
        return f"http://{ip}:{self.port}" if ip else ""

    def _resolve_gateway_ip(self) -> str:
        """The gateway's agent-network address, or "" while it has none."""
        return container_mod.try_container_ipv4_on_network(
            self._gateway_name, self.network)

    def is_healthy(
        self, url: str, *, timeout: float = _HEALTH_REQUEST_TIMEOUT_SECONDS,
    ) -> bool:
        if not url:
            return False
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _orchestrator_source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator was created from the current
        bind-mounted control-plane source (it loads that code at startup and
        won't reload it)."""
        if not container_mod.container_is_running(self._orchestrator_name):
            return False
        env = container_mod.container_env(self._orchestrator_name)
        if not env:
            return True  # can't compare → don't churn a working container
        return env.get("BOT_BOTTLE_SOURCE_HASH") == current_hash

    def ensure_built(self) -> None:
        """Ensure the gateway + orchestrator images exist. The control-plane
        source is bind-mounted, so a code change takes effect without a rebuild;
        the images still carry the package for their entrypoints."""
        container_mod.build_image(
            self.gateway_image, str(self._repo_root), dockerfile="Dockerfile.gateway")
        container_mod.build_image(
            self.orchestrator_image, str(self._repo_root),
            dockerfile="Dockerfile.orchestrator")

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> InfraEndpoint:
        """Ensure the orchestrator + gateway containers are up; return how to
        reach them. Idempotent per-host singleton — a healthy orchestrator on
        current source is left untouched. Raises `OrchestratorStartError` on
        control-plane startup timeout."""
        self.ensure_built()
        ensure_networks(self.network, self.egress_network, self.control_network)

        current_hash = source_hash(self._repo_root)
        url = self._resolve_orchestrator_url()
        if not (self._orchestrator_source_current(current_hash)
                and url and self.is_healthy(url)):
            log.info("starting orchestrator container",
                     context={"name": self._orchestrator_name})
            self._run_orchestrator_container(current_hash)
            url = self._wait_healthy(startup_timeout)

        # (Re)ensure the gateway once the control plane it resolves against is
        # healthy — it needs the orchestrator's control-network address.
        self._ensure_gateway_container(url)
        return InfraEndpoint(orchestrator_url=url, gateway_ip=self._resolve_gateway_ip())

    def _run_orchestrator_container(self, current_hash: str) -> None:
        container_mod.force_remove_container(self._orchestrator_name)
        _signing_key = host_orchestrator_token()
        argv = [
            "container", "run", "--detach",
            "--name", self._orchestrator_name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", ORCHESTRATOR_LABEL,
            # Control network only — agents are never on it (L3-isolated).
            "--network", self.control_network,
            "--dns", container_mod.dns_server(),
            # Container-only DB volume: exactly one kernel writes bot-bottle.db.
            "--volume", f"{self._db_volume}:{_DB_ROOT_IN_CONTAINER}",
            # Live control-plane source (a code change takes effect on relaunch).
            "--mount",
            container_mod.bind_mount_spec(
                str(self._repo_root), _SRC_IN_CONTAINER, readonly=True),
            "--env", f"PYTHONPATH={_SRC_IN_CONTAINER}",
            "--env", f"BOT_BOTTLE_ROOT={_DB_ROOT_IN_CONTAINER}",
            # Detect a real control-plane code change and recreate.
            "--env", f"BOT_BOTTLE_SOURCE_HASH={current_hash}",
            # The signing key — held ONLY by the orchestrator (issue #469). Bare
            # `--env NAME` keeps the value off argv / `container inspect`.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            self.orchestrator_image,
            # Dockerfile.orchestrator ENTRYPOINT is `-m bot_bottle.orchestrator`.
            "--host", "0.0.0.0", "--port", str(self.port), "--broker", "stub",
        ]
        result = container_mod.run_container_argv(
            argv, env={**os.environ, ORCHESTRATOR_TOKEN_ENV: _signing_key})
        if result.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def _ensure_gateway_container(self, orchestrator_url: str) -> None:
        """Start (recreate) the gateway container, dual-homed on the agent +
        control networks, resolving policy against `orchestrator_url` (the
        orchestrator's control-network address — Apple has no DNS)."""
        container_mod.force_remove_container(self._gateway_name)
        _signing_key = host_orchestrator_token()
        argv = [
            "container", "run", "--detach",
            "--name", self._gateway_name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", INFRA_LABEL,
            # NAT egress FIRST (default route out); the host-only agent network
            # is where agents reach the gateway; the control network reaches the
            # orchestrator.
            "--network", self.egress_network,
            "--network", self.network,
            "--network", self.control_network,
            "--dns", container_mod.dns_server(),
            # The mitmproxy CA on a host bind-mount (survives recreation +
            # volume pruning — issue #450). No DB mount: the data plane never
            # opens bot-bottle.db (#469).
            "--mount",
            container_mod.bind_mount_spec(str(host_gateway_ca_dir()), MITMPROXY_HOME),
            "--env", f"BOT_BOTTLE_GATEWAY_DAEMONS={_GATEWAY_DAEMONS}",
            "--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={orchestrator_url}",
            # The pre-minted `gateway` JWT (never the signing key). Bare
            # `--env NAME` inherits the value from run_env below.
            "--env", ORCHESTRATOR_AUTH_JWT_ENV,
            self.gateway_image,
        ]
        run_env = {**os.environ, ORCHESTRATOR_AUTH_JWT_ENV: mint(ROLE_GATEWAY, _signing_key)}
        result = container_mod.run_container_argv(argv, env=run_env)
        if result.returncode != 0:
            raise GatewayError(
                f"gateway container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def _wait_healthy(self, startup_timeout: float) -> str:
        deadline = time.monotonic() + startup_timeout
        while True:
            url = self._resolve_orchestrator_url()
            if url and self.is_healthy(url):
                log.info("orchestrator healthy", context={"url": url})
                return url
            if time.monotonic() >= deadline:
                raise OrchestratorStartError(
                    f"orchestrator did not become healthy within "
                    f"{startup_timeout:g}s"
                )
            time.sleep(_HEALTH_POLL_SECONDS)

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception. Read from the gateway container; polls because mitmproxy
        writes it a beat after start."""
        def _fetch() -> str | None:
            result = container_mod.run_container_argv(
                ["container", "exec", self._gateway_name, "cat", GATEWAY_CA_CERT])
            return result.stdout if result.returncode == 0 and result.stdout.strip() else None
        try:
            return backend_util.poll_ca_cert(_fetch, timeout=timeout)
        except TimeoutError as exc:
            raise GatewayError(
                f"gateway CA not available in {self._gateway_name} after {timeout:g}s"
            ) from exc

    def stop(self) -> None:
        """Remove both containers (idempotent). The DB volume persists."""
        container_mod.force_remove_container(self._gateway_name)
        container_mod.force_remove_container(self._orchestrator_name)


def probe_orchestrator_url(port: int = DEFAULT_PORT) -> str:
    """The running orchestrator's control-plane URL, or "" if it isn't up. Used
    by host-side control-plane discovery; safe on any host (returns "" when the
    container or the `container` CLI isn't present)."""
    ip = container_mod.try_container_ipv4_on_network(ORCHESTRATOR_NAME, CONTROL_NETWORK)
    return f"http://{ip}:{port}" if ip else ""


__all__ = [
    "MacosInfraService",
    "InfraEndpoint",
    "OrchestratorStartError",
    "GatewayError",
    "ORCHESTRATOR_NAME",
    "INFRA_NAME",
    "INFRA_DB_VOLUME",
]
