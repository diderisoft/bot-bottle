"""The per-host control plane + gateway for the docker backend (PRD 0070).

Runs the orchestrator (control plane) and the gateway (data plane) as **two
separate containers**, split now that #469 got the DB off the data plane:

  * `bot-bottle-orchestrator` — the lean control-plane container. Joins the
    `bot-bottle-orchestrator` **control network** (`--internal`) only, plus a
    host-loopback publish for the CLI. Sole opener of `bot-bottle.db`; holds the
    signing key.
  * `bot-bottle-gateway` — the data-plane container (`DockerGateway`).
    **Dual-homed** on the agent-facing `bot-bottle-gateway` network *and* the
    control network, so it reaches the orchestrator by name over docker DNS
    (`http://bot-bottle-orchestrator:8099`) while agents — which are never on
    the control network — cannot reach the control plane at all (the L3 block
    Firecracker's nft already has). Holds the `gateway` JWT + the mitmproxy CA.

`DockerInfraService` brings both up as an idempotent per-host pair. Callers use
`ensure_running()` (returns the host control-plane URL) + `gateway_name` (the
container the launch flow attributes agents against).
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ... import log
from .util import run_docker
from .gateway import DockerGateway
from ...paths import (
    ORCHESTRATOR_TOKEN_ENV,
    bot_bottle_root,
    host_orchestrator_token,
)
from ...gateway import (
    GATEWAY_DOCKERFILE,
    GATEWAY_IMAGE,
    GATEWAY_NAME,
    GATEWAY_NETWORK,
    GatewayError,
)
from ...orchestrator_auth import ROLE_GATEWAY, mint
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    OrchestratorStartError,
    source_hash,
)

# The control plane's own container + the dedicated control network the gateway
# reaches it over. The network is created `--internal`: the orchestrator and
# gateway join it, agents never do, so agents have no route to the control
# plane (PRD 0070 "Separating the planes"). Same name across docker + macOS.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-orchestrator=1"
ORCHESTRATOR_NETWORK = "bot-bottle-orchestrator"
ORCHESTRATOR_IMAGE = os.environ.get(
    "BOT_BOTTLE_ORCHESTRATOR_IMAGE", "bot-bottle-orchestrator:latest"
)
ORCHESTRATOR_DOCKERFILE = "Dockerfile.orchestrator"
# Baked as a container label so `ensure_running` can detect whether the running
# orchestrator is executing the current bind-mounted source.
ORCHESTRATOR_SOURCE_HASH_LABEL = "bot-bottle-orchestrator-source-hash"

# The bind-mount path for the live control-plane source inside the orchestrator
# container. PYTHONPATH points here so a code change takes effect on the next
# launch without an image rebuild.
_SRC_IN_CONTAINER = "/bot-bottle-src"
# Bot-bottle host-root bind-mount (DB + state) inside the orchestrator. The
# control plane opens bot-bottle.db under here (via BOT_BOTTLE_ROOT ->
# host_db_path()); it is the ONLY container with a handle on it (issue #469).
_ROOT_IN_CONTAINER = "/bot-bottle-root"

# The URL the gateway's data plane resolves policy against — the orchestrator
# container by name on the control network (docker DNS, container↔container).
ORCHESTRATOR_URL_IN_NETWORK = f"http://{ORCHESTRATOR_NAME}:{DEFAULT_PORT}"

# Back-compat aliases: the split retired the combined `bot-bottle-infra`
# container, but the fixed-name constants some callers/tests import still map to
# the pair's public identity.
INFRA_NAME = GATEWAY_NAME  # the container agents attribute against is the gateway

_HEALTH_POLL_SECONDS = 0.25
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0

_REPO_ROOT = Path(__file__).resolve().parents[3]


class DockerInfraService:
    """Brings up the per-host control plane + gateway as two containers.

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
        repo_root: Path = _REPO_ROOT,
        host_root: Path | None = None,
        orchestrator_name: str = ORCHESTRATOR_NAME,
        orchestrator_label: str = ORCHESTRATOR_LABEL,
        gateway_name: str = GATEWAY_NAME,
    ) -> None:
        self.port = port
        self.network = network
        self.control_network = control_network
        self.orchestrator_image = orchestrator_image
        self.gateway_image = gateway_image
        self._repo_root = repo_root
        self._host_root = host_root or bot_bottle_root()
        self._orchestrator_name = orchestrator_name
        self._orchestrator_label = orchestrator_label
        self._gateway_name = gateway_name

    @property
    def gateway_name(self) -> str:
        """The gateway container — the address the launch flow attributes agents
        against (gateway IP, git-gate provisioning transport, CA fetch)."""
        return self._gateway_name

    @property
    def url(self) -> str:
        """Host-side control-plane URL (the orchestrator's published loopback)."""
        return f"http://127.0.0.1:{self.port}"

    def is_healthy(self, *, timeout: float = _HEALTH_REQUEST_TIMEOUT_SECONDS) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _container_running(self, name: str) -> bool:
        proc = run_docker(["docker", "ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"])
        return name in proc.stdout.split()

    def _orchestrator_source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator was started from the current
        bind-mounted source."""
        if not self._container_running(self._orchestrator_name):
            return False
        proc = run_docker([
            "docker", "inspect", "--format",
            "{{ index .Config.Labels \"" + ORCHESTRATOR_SOURCE_HASH_LABEL + "\" }}",
            self._orchestrator_name,
        ])
        if proc.returncode != 0:
            return True  # can't compare → don't churn a working container
        return proc.stdout.strip() == current_hash

    def _ensure_network(self, name: str, *, internal: bool) -> None:
        if run_docker(["docker", "network", "inspect", name]).returncode == 0:
            return
        argv = ["docker", "network", "create"]
        if internal:
            argv.append("--internal")
        argv.append(name)
        proc = run_docker(argv)
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(f"network {name} failed to create: {proc.stderr.strip()}")

    def _build_images(self) -> None:
        """Build the gateway (data plane) and orchestrator (control plane)
        images. Cache-aware: a no-op when nothing changed. The combined
        `Dockerfile.infra` is no longer built — the planes ship as two images."""
        for tag, dockerfile in (
            (self.gateway_image, GATEWAY_DOCKERFILE),
            (self.orchestrator_image, ORCHESTRATOR_DOCKERFILE),
        ):
            argv = ["docker", "build", "-t", tag,
                    "-f", str(self._repo_root / dockerfile),
                    str(self._repo_root)]
            if os.environ.get("BOT_BOTTLE_NO_CACHE"):
                argv.insert(2, "--no-cache")
            proc = run_docker(argv)
            if proc.returncode != 0:
                raise GatewayError(f"{dockerfile} build failed: {proc.stderr.strip()}")

    def _run_orchestrator_container(self, current_hash: str) -> None:
        """Start the lean control-plane container on the control network only,
        published to host loopback for the CLI. Idempotent (clears a stale
        fixed-name container first)."""
        self._ensure_network(self.control_network, internal=True)
        run_docker(["docker", "rm", "--force", self._orchestrator_name])
        _signing_key = host_orchestrator_token()
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self._orchestrator_name,
            "--label", self._orchestrator_label,
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current_hash}",
            # Control network only — agents are never on it, so they have no
            # route to the control plane (the L3 block, not just the JWT).
            "--network", self.control_network,
            # Host CLI reaches the control plane here (loopback only). The
            # orchestrator listens on the fixed DEFAULT_PORT inside the
            # container; self.port is the host-side published port.
            "--publish", f"127.0.0.1:{self.port}:{DEFAULT_PORT}",
            # Live control-plane source (code changes without an image rebuild).
            "--volume", f"{self._repo_root}:{_SRC_IN_CONTAINER}:ro",
            "--env", f"PYTHONPATH={_SRC_IN_CONTAINER}",
            # Orchestrator registry DB on the host (sole writer: control plane).
            "--volume", f"{self._host_root}:{_ROOT_IN_CONTAINER}",
            "--env", f"BOT_BOTTLE_ROOT={_ROOT_IN_CONTAINER}",
            # The signing key — held ONLY by the orchestrator (it verifies
            # tokens); the gateway gets the pre-minted `gateway` JWT, never the
            # key (issue #469). Bare `--env NAME` keeps the value off argv.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            self.orchestrator_image,
            # Dockerfile.orchestrator ENTRYPOINT is `python3 -m bot_bottle.orchestrator`;
            # these are its args.
            "--host", "0.0.0.0", "--port", str(DEFAULT_PORT), "--broker", "stub",
        ], env={**os.environ, ORCHESTRATOR_TOKEN_ENV: _signing_key})
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: {proc.stderr.strip()}"
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
            dockerfile=None,  # images are built by _build_images
        )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Ensure the orchestrator + gateway containers are up; return the host
        control-plane URL. Idempotent — a healthy orchestrator on current source
        (and a current-image gateway) is left untouched. Raises
        `OrchestratorStartError` on control-plane startup timeout."""
        self._build_images()
        self._ensure_network(self.network, internal=False)

        current_hash = source_hash(self._repo_root)
        if not (self.is_healthy() and self._orchestrator_source_current(current_hash)):
            log.info("starting orchestrator container",
                     context={"name": self._orchestrator_name})
            self._run_orchestrator_container(current_hash)

            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if self.is_healthy():
                    log.info("orchestrator healthy", context={"url": self.url})
                    break
                time.sleep(_HEALTH_POLL_SECONDS)
            else:
                raise OrchestratorStartError(
                    f"orchestrator at {self.url} did not become healthy "
                    f"within {startup_timeout:g}s"
                )

        # Bring up (or refresh) the gateway once the control plane it resolves
        # against is healthy. The orchestrator (which holds the signing key)
        # mints the role-scoped `gateway` JWT here and hands it to the gateway,
        # which never sees the key (#469). `connect_to_orchestrator` is
        # idempotent.
        gateway_token = mint(ROLE_GATEWAY, host_orchestrator_token())
        self.gateway().connect_to_orchestrator(
            ORCHESTRATOR_URL_IN_NETWORK, gateway_token,
        )
        return self.url

    def stop(self) -> None:
        """Remove both containers (idempotent)."""
        run_docker(["docker", "rm", "--force", self._gateway_name])
        run_docker(["docker", "rm", "--force", self._orchestrator_name])


__all__ = [
    "DockerInfraService",
    "ORCHESTRATOR_NAME",
    "ORCHESTRATOR_NETWORK",
    "ORCHESTRATOR_IMAGE",
    "ORCHESTRATOR_SOURCE_HASH_LABEL",
    "INFRA_NAME",
]
