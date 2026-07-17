"""Orchestrator + gateway lifecycle for the macOS backend (PRD 0070).

The macOS counterpart of `orchestrator.lifecycle.OrchestratorService`. Same
shape — an idempotent singleton that brings up the control plane and the shared
gateway and hands back the control-plane URL — but the startup **order is
reversed**, and the reason is a real Apple Container constraint rather than a
stylistic choice:

- Docker starts the gateway first and lets it find the control plane by
  *container name* over docker DNS.
- Apple Container 1.0.0 has **no container DNS** (see `gateway`), so the
  gateway can only be handed an **IP**. That IP does not exist until the
  orchestrator container is running — hence: orchestrator first, read its
  address, then start the gateway pointed at it.

The second difference: no published port. Apple Container puts every container
on a host-reachable address, and the host can reach the host-only network
directly, so the host CLI and the gateway use the **same** URL — the
orchestrator's address on the shared network. Docker needs a
`--publish 127.0.0.1:…` hop plus a separate `internal_url` for the same job.

Like the docker service this runs the **register-only broker**: the backend
launches agent containers, so the control plane needs no privileged socket.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

from ... import log
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ORCHESTRATOR_DOCKERFILE,
    ORCHESTRATOR_IMAGE,
    ORCHESTRATOR_SOURCE_HASH_LABEL,
    OrchestratorStartError,
    source_hash,
)
from ...paths import bot_bottle_root
from . import util as container_mod
from .gateway import (
    GATEWAY_EGRESS_NETWORK,
    GATEWAY_IMAGE,
    GATEWAY_NETWORK,
    AppleGateway,
    ensure_networks,
)
from .util import bind_mount_spec as _mount

# Distinct from the docker backend's container name so both can run on one host.
ORCHESTRATOR_NAME = "bot-bottle-mac-orchestrator"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_DIR = "/app"
_ROOT_IN_CONTAINER = "/bot-bottle-root"

_HEALTH_POLL_SECONDS = 0.25
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0


class MacosOrchestratorService:
    """Manages the orchestrator control-plane container + the shared Apple
    gateway. Callers only need `ensure_running()`, which returns the URL."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        image: str = ORCHESTRATOR_IMAGE,
        gateway_image: str = GATEWAY_IMAGE,
        repo_root: Path = _REPO_ROOT,
        host_root: Path | None = None,
        orchestrator_name: str = ORCHESTRATOR_NAME,
    ) -> None:
        self.port = port
        self.network = network
        self.egress_network = egress_network
        self.image = image
        self._gateway_image = gateway_image
        self._repo_root = repo_root
        self._host_root = host_root or bot_bottle_root()
        self._orchestrator_name = orchestrator_name

    def _resolve_url(self) -> str:
        """The control-plane URL, or "" while the container has no address.
        Resolved fresh each time (there is no name to fall back on, and a
        recreated orchestrator can come back on a different DHCP address), so
        nothing caches a URL that could go stale."""
        ip = container_mod.try_container_ipv4_on_network(
            self._orchestrator_name, self.network,
        )
        return f"http://{ip}:{self.port}" if ip else ""

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

    def _ensure_orchestrator_image(self) -> None:
        """Build the lean control-plane image when missing. Build-if-missing,
        not build-every-time: the control plane bind-mounts its source, so a
        code change is caught by the source-hash recreate, not a rebuild."""
        if container_mod.image_exists(self.image):
            return
        container_mod.build_image(
            self.image, str(self._repo_root), dockerfile=ORCHESTRATOR_DOCKERFILE,
        )

    def _orchestrator_source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator was created from the *current*
        bind-mounted source. The process loaded that code at startup and won't
        reload it, so a stale container would keep serving OLD control-plane
        code."""
        if not container_mod.container_is_running(self._orchestrator_name):
            return False
        data = container_mod.inspect_container(self._orchestrator_name)
        config = data.get("configuration")
        labels = config.get("labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            return True  # can't compare → don't churn a working container
        return labels.get(ORCHESTRATOR_SOURCE_HASH_LABEL) == current_hash

    def _run_orchestrator_container(self, current_hash: str) -> None:
        # The orchestrator is the first thing on the shared network, so it —
        # not the gateway — is what has to bring the network into existence.
        ensure_networks(self.network, self.egress_network)
        container_mod.force_remove_container(self._orchestrator_name)
        argv = [
            "container", "run", "--detach",
            "--name", self._orchestrator_name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current_hash}",
            # Host-only network only: the control plane needs no route out, and
            # the host reaches it here directly (no --publish needed).
            "--network", self.network,
            "--mount", _mount(str(self._repo_root), _APP_DIR, readonly=True),
            "--workdir", _APP_DIR,
            # Persist the registry DB on the host (sole-owner: only the
            # orchestrator opens bot-bottle.db).
            "--mount", _mount(str(self._host_root), _ROOT_IN_CONTAINER),
            "--env", f"BOT_BOTTLE_ROOT={_ROOT_IN_CONTAINER}",
            "--entrypoint", "python3",
            self.image,
            "-m", "bot_bottle.orchestrator",
            "--host", "0.0.0.0", "--port", str(self.port), "--broker", "stub",
        ]
        result = container_mod.run_container_argv(argv)
        if result.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def gateway(self, orchestrator_url: str) -> AppleGateway:
        """The shared gateway, pointed at the control plane at
        `orchestrator_url` (by IP — Apple has no container DNS)."""
        return AppleGateway(
            self._gateway_image,
            network=self.network,
            egress_network=self.egress_network,
            orchestrator_url=orchestrator_url,
        )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Ensure the control plane + shared gateway are up; return the
        control-plane URL. Idempotent — a healthy control plane running current
        code and a running gateway are left untouched."""
        current_hash = source_hash(self._repo_root)
        url = self._running_healthy_url(current_hash)
        if not url:
            self._ensure_orchestrator_image()
            log.info(
                "starting orchestrator container",
                context={"name": self._orchestrator_name},
            )
            self._run_orchestrator_container(current_hash)
            url = self._wait_healthy(startup_timeout)

        # Gateway second: it can only reach the control plane by IP, which does
        # not exist until the orchestrator container is up (see the docstring).
        gateway = self.gateway(url)
        gateway.ensure_built()
        gateway.ensure_running()
        return url

    def _running_healthy_url(self, current_hash: str) -> str:
        """The control-plane URL if the running orchestrator is BOTH current
        and answering /health, else "" (→ recreate).

        Checking health, not just the source-hash label, is what lets the
        service self-heal: a container that is running the current code but
        whose HTTP server is wedged (bind failure, deadlock, OOM'd thread)
        would otherwise be left alone and polled to death on every launch
        forever. Mirrors the docker service's `is_healthy() and source_current`
        gate."""
        if not self._orchestrator_source_current(current_hash):
            return ""
        url = self._resolve_url()
        return url if url and self.is_healthy(url) else ""

    def _wait_healthy(self, startup_timeout: float) -> str:
        """Poll until the control plane answers /health, resolving its address
        each time: the container is up before it has an IP, and it has an IP
        before the server binds."""
        deadline = time.monotonic() + startup_timeout
        while True:
            url = self._resolve_url()
            if url and self.is_healthy(url):
                log.info("orchestrator healthy", context={"url": url})
                return url
            if time.monotonic() >= deadline:
                raise OrchestratorStartError(
                    f"orchestrator did not become healthy within "
                    f"{startup_timeout:g}s"
                )
            time.sleep(_HEALTH_POLL_SECONDS)

    def stop(self) -> None:
        """Remove the orchestrator + gateway containers (idempotent)."""
        container_mod.force_remove_container(self._orchestrator_name)
        self.gateway("").stop()


__all__ = [
    "MacosOrchestratorService",
    "OrchestratorStartError",
    "ORCHESTRATOR_NAME",
]
