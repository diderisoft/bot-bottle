"""Orchestrator + gateway lifecycle (PRD 0070, docker slice).

Runs the orchestrator control plane **as a container** on the shared gateway
network, alongside the gateway container. This is the PRD's "virtualize the
orchestrator": container↔container between the gateway and the orchestrator
avoids the host firewall (which drops container→host traffic), and the gateway
reaches the control plane by container name over docker DNS. The host CLI
reaches it via a published loopback port.

The orchestrator runs with the **register-only broker** — the *backend*
launches agent containers (compose), so the orchestrator needs no docker
socket. That keeps this control-plane container unprivileged; the host manages
both containers. `ensure_running` is an idempotent singleton (fixed container
names + the published port).
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import log
from ..docker_cmd import run_docker
from ..paths import bot_bottle_root
from .gateway import GATEWAY_IMAGE, GATEWAY_NETWORK, DockerGateway

DEFAULT_PORT = 8099
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-orchestrator=1"
# Baked onto the container as a label so `ensure_running` can tell whether the
# running process is executing the *current* bind-mounted source — see
# `_source_hash`.
ORCHESTRATOR_SOURCE_HASH_LABEL = "bot-bottle-orchestrator-source-hash"

# The repo root is bind-mounted into the control-plane container so
# `python -m bot_bottle.orchestrator` resolves the package (the orchestrator
# is stdlib-only, so the bundle image's python is enough).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = "/app"
_ROOT_IN_CONTAINER = "/bot-bottle-root"

_HEALTH_POLL_SECONDS = 0.25
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0


class OrchestratorStartError(RuntimeError):
    """The orchestrator container did not become healthy within the timeout."""


def _source_hash(repo_root: Path) -> str:
    """Content hash of the orchestrator's bind-mounted Python source (the
    `bot_bottle` package the control-plane process imports). This only
    changes when the code that would actually run inside the container
    changes — `ensure_running` recreates the container on a mismatch and
    otherwise leaves a healthy one alone, so a bottle launch that isn't
    accompanied by a code change doesn't restart the process and drop every
    *other* active bottle's in-memory egress tokens (`Orchestrator._tokens`
    in `service.py`, never persisted to disk by design)."""
    h = hashlib.sha256()
    for path in sorted((repo_root / "bot_bottle").rglob("*.py")):
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


class OrchestratorService:
    """Manages the orchestrator control-plane container + the shared gateway.
    Callers only need `ensure_running()` + `url`."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        image: str = GATEWAY_IMAGE,
        repo_root: Path = _REPO_ROOT,
        host_root: Path | None = None,
    ) -> None:
        self.port = port
        self.network = network
        self.image = image
        self._repo_root = repo_root
        self._host_root = host_root or bot_bottle_root()

    @property
    def url(self) -> str:
        """Host-side control-plane URL (published loopback port)."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def internal_url(self) -> str:
        """Control-plane URL as the gateway container reaches it — by name over
        docker DNS on the shared network. This is the gateway's
        BOT_BOTTLE_ORCHESTRATOR_URL."""
        return f"http://{ORCHESTRATOR_NAME}:{self.port}"

    def is_healthy(self, *, timeout: float = _HEALTH_REQUEST_TIMEOUT_SECONDS) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _container_running(self, name: str) -> bool:
        proc = run_docker(["docker", "ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"])
        return name in proc.stdout.split()

    def _run_orchestrator_container(self, source_hash: str) -> None:
        """Start the control-plane container (idempotent: clears a stale
        fixed-name container first). Register-only broker → no docker socket.
        Labels the container with `source_hash` so a later `ensure_running`
        can detect a real code change (see `_source_hash`)."""
        run_docker(["docker", "rm", "--force", ORCHESTRATOR_NAME])
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", ORCHESTRATOR_NAME,
            "--label", ORCHESTRATOR_LABEL,
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={source_hash}",
            "--network", self.network,
            # Host CLI reaches the control plane here; bound to loopback so it
            # is not exposed on the host's external interfaces.
            "--publish", f"127.0.0.1:{self.port}:{self.port}",
            "--volume", f"{self._repo_root}:{_APP_DIR}:ro",
            "--workdir", _APP_DIR,
            # Persist the registry DB on the host (sole-owner: only the
            # orchestrator opens bot-bottle.db).
            "--volume", f"{self._host_root}:{_ROOT_IN_CONTAINER}",
            "--env", f"BOT_BOTTLE_ROOT={_ROOT_IN_CONTAINER}",
            "--entrypoint", "python3",
            self.image,
            "-m", "bot_bottle.orchestrator",
            "--host", "0.0.0.0", "--port", str(self.port), "--broker", "stub",
        ])
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: {proc.stderr.strip()}"
            )

    def _gateway(self) -> DockerGateway:
        return DockerGateway(self.image, network=self.network, orchestrator_url=self.internal_url)

    def _orchestrator_source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator container was created from the
        *current* bind-mounted source. Mirrors `DockerGateway`'s
        image-staleness check, but by content hash rather than image id since
        the orchestrator runs bind-mounted source, not a built image."""
        if not self._container_running(ORCHESTRATOR_NAME):
            return False
        proc = run_docker([
            "docker", "inspect", "--format",
            "{{ index .Config.Labels \"" + ORCHESTRATOR_SOURCE_HASH_LABEL + "\" }}",
            ORCHESTRATOR_NAME,
        ])
        if proc.returncode != 0:
            return True  # can't compare -> don't churn a working container
        return proc.stdout.strip() == current_hash

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Ensure the control plane + shared gateway are up; return the host
        control-plane URL. Idempotent — a healthy control plane running
        current code and a running gateway are left untouched. Raises
        `OrchestratorStartError` on timeout."""
        gateway = self._gateway()
        gateway.ensure_built()  # rebuild the bundle image on a source change
        gateway.ensure_running()  # creates the shared network + (re)starts gateway

        # Recreate the orchestrator container only when its bind-mounted
        # source has actually changed since it started — its Python process
        # loaded that code at startup and won't reload, so a stale container
        # would keep running OLD control-plane code. Recreating on *every*
        # launch (the prior behaviour) would drop every other active
        # bottle's in-memory egress tokens each time a new bottle starts,
        # since the orchestrator process holds them only in memory (#381).
        current_hash = _source_hash(self._repo_root)
        if self.is_healthy() and self._orchestrator_source_current(current_hash):
            return self.url

        log.info("starting orchestrator container", context={"name": ORCHESTRATOR_NAME})
        self._run_orchestrator_container(current_hash)

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.is_healthy():
                log.info("orchestrator healthy", context={"url": self.url})
                return self.url
            time.sleep(_HEALTH_POLL_SECONDS)
        raise OrchestratorStartError(
            f"orchestrator at {self.url} did not become healthy within {startup_timeout:g}s"
        )

    def stop(self) -> None:
        """Remove the orchestrator + gateway containers (idempotent)."""
        run_docker(["docker", "rm", "--force", ORCHESTRATOR_NAME])
        self._gateway().stop()


__all__ = [
    "OrchestratorService",
    "OrchestratorStartError",
    "ORCHESTRATOR_NAME",
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
]
