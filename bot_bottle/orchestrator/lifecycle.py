"""Orchestrator + gateway lifecycle (PRD 0070, docker slice).

Runs both the orchestrator control plane and the gateway data plane inside
a single `bot-bottle-infra` container on the shared gateway network —
matching the structure already used by the macOS and Firecracker backends.
`gateway_init` is PID 1 and supervises both; the infra container is an
idempotent per-host singleton.

The combined container replaces the prior two-container split
(bot-bottle-orchestrator + bot-bottle-orch-gateway). The host CLI reaches
the control plane via a published loopback port; gateway daemons reach it
over 127.0.0.1 (same container).
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import log
from ..control_auth import ROLE_GATEWAY, mint
from ..docker_cmd import run_docker
from ..paths import (
    CONTROL_AUTH_JWT_ENV,
    CONTROL_PLANE_TOKEN_ENV,
    bot_bottle_root,
    host_control_plane_token,
    host_gateway_ca_dir,
)
from ..gateway import (
    GATEWAY_DOCKERFILE,
    GATEWAY_IMAGE,
    GATEWAY_NETWORK,
    GatewayError,
    MITMPROXY_HOME,
)

DEFAULT_PORT = 8099
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0

INFRA_NAME = "bot-bottle-infra"
INFRA_LABEL = "bot-bottle-infra=1"
# The combined infra image: gateway data plane + orchestrator content.
# Built from Dockerfile.infra (FROM gateway + COPY --from orchestrator).
INFRA_IMAGE = os.environ.get("BOT_BOTTLE_INFRA_IMAGE", "bot-bottle-infra:latest")
INFRA_DOCKERFILE = "Dockerfile.infra"
# Baked as a container label so `ensure_running` can detect whether the
# running container is executing the current bind-mounted source.
INFRA_SOURCE_HASH_LABEL = "bot-bottle-infra-source-hash"

# Orchestrator image: the single canonical definition of the control-plane
# content (lean: python:3.12-slim + bot_bottle package, no mitmproxy/git).
# Used as a build intermediate: `Dockerfile.infra` COPY --from this image.
ORCHESTRATOR_IMAGE = os.environ.get(
    "BOT_BOTTLE_ORCHESTRATOR_IMAGE", "bot-bottle-orchestrator:latest"
)
ORCHESTRATOR_DOCKERFILE = "Dockerfile.orchestrator"

# The gateway daemons + orchestrator the infra container runs.
# BOT_BOTTLE_GATEWAY_DAEMONS listing `orchestrator` opts it in to
# gateway_init's supervise tree (see gateway_init._OPT_IN_DAEMONS).
_INFRA_DAEMONS = "egress,git-http,supervise,orchestrator"

# The bind-mount path for the live control-plane source inside the
# container. Separate from /app so the gateway's baked scripts
# (egress_addon.py, egress-entrypoint.sh) are not overlaid.
_SRC_IN_CONTAINER = "/bot-bottle-src"
# Bot-bottle host-root bind-mount inside the container (DB + state). The
# orchestrator control plane opens bot-bottle.db under here (via BOT_BOTTLE_ROOT
# -> host_db_path()); it is the ONLY process in the container with a file
# handle on it (PRD 0070 / issue #469).
_ROOT_IN_CONTAINER = "/bot-bottle-root"

_HEALTH_POLL_SECONDS = 0.25
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0

_REPO_ROOT = Path(__file__).resolve().parents[2]


class OrchestratorStartError(RuntimeError):
    """The infra container did not become healthy within the timeout."""


def source_hash(repo_root: Path) -> str:
    """Content hash of the orchestrator's bind-mounted Python source (the
    `bot_bottle` package the control-plane process imports). Changes only
    when the code that would actually run changes — `ensure_running`
    recreates the container on a mismatch so a code change takes effect,
    but leaves a healthy up-to-date container alone to preserve in-memory
    egress tokens."""
    h = hashlib.sha256()
    for path in sorted((repo_root / "bot_bottle").rglob("*.py")):
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


class OrchestratorService:
    """Manages the single per-host infra container (control plane + gateway).
    Callers only need `ensure_running()` + `url`.

    `infra_name` / `infra_label` let backends run independent infra containers
    on the same host without name collisions (e.g. isolated integration tests
    that can't share the production INFRA_NAME singleton)."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        image: str = INFRA_IMAGE,
        repo_root: Path = _REPO_ROOT,
        host_root: Path | None = None,
        infra_name: str = INFRA_NAME,
        infra_label: str = INFRA_LABEL,
    ) -> None:
        self.port = port
        self.network = network
        self.image = image
        self._repo_root = repo_root
        self._host_root = host_root or bot_bottle_root()
        self._infra_name = infra_name
        self._infra_label = infra_label

    @property
    def url(self) -> str:
        """Host-side control-plane URL (published loopback port)."""
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

    def _infra_source_current(self, current_hash: str) -> bool:
        """True iff the running infra container was started from the current
        bind-mounted source. Mirrors the macOS backend's `_source_current`."""
        if not self._container_running(self._infra_name):
            return False
        proc = run_docker([
            "docker", "inspect", "--format",
            "{{ index .Config.Labels \"" + INFRA_SOURCE_HASH_LABEL + "\" }}",
            self._infra_name,
        ])
        if proc.returncode != 0:
            return True  # can't compare → don't churn a working container
        return proc.stdout.strip() == current_hash

    def _ensure_network(self) -> None:
        if run_docker(["docker", "network", "inspect", self.network]).returncode == 0:
            return
        proc = run_docker(["docker", "network", "create", self.network])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway network {self.network} failed to create: {proc.stderr.strip()}"
            )

    def _build_images(self) -> None:
        """Build the gateway base, the orchestrator intermediate, then the
        infra image. All are cache-aware: a no-op when nothing changed."""
        for tag, dockerfile in (
            (GATEWAY_IMAGE, GATEWAY_DOCKERFILE),
            (ORCHESTRATOR_IMAGE, ORCHESTRATOR_DOCKERFILE),
            (self.image, INFRA_DOCKERFILE),
        ):
            argv = ["docker", "build", "-t", tag,
                    "-f", str(self._repo_root / dockerfile),
                    str(self._repo_root)]
            if os.environ.get("BOT_BOTTLE_NO_CACHE"):
                argv.insert(2, "--no-cache")
            proc = run_docker(argv)
            if proc.returncode != 0:
                raise GatewayError(f"{dockerfile} build failed: {proc.stderr.strip()}")

    def _run_infra_container(self, current_hash: str) -> None:
        """Start the combined infra container (idempotent: clears a stale
        fixed-name container first). Labels the container with `current_hash`
        so a later `ensure_running` can detect a real code change."""
        self._ensure_network()
        run_docker(["docker", "rm", "--force", self._infra_name])
        _signing_key = host_control_plane_token()
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self._infra_name,
            "--label", self._infra_label,
            "--label", f"{INFRA_SOURCE_HASH_LABEL}={current_hash}",
            "--network", self.network,
            # Host CLI reaches the control plane here (loopback only).
            # gateway_init always starts the orchestrator on DEFAULT_PORT (8099)
            # inside the container; self.port is the host-side published port.
            "--publish", f"127.0.0.1:{self.port}:{DEFAULT_PORT}",
            # Persist the mitmproxy CA on the host so it survives container
            # recreation AND docker volume pruning (issue #450): every agent
            # trusts this one CA, so a fresh one would break all running bottles.
            "--volume", f"{host_gateway_ca_dir()}:{MITMPROXY_HOME}",
            # Live control-plane source, mounted to a path that does not
            # overlay the gateway's baked /app scripts.
            "--volume", f"{self._repo_root}:{_SRC_IN_CONTAINER}:ro",
            # PYTHONPATH lets the orchestrator (and other Python daemons)
            # import the live source ahead of the installed package.
            "--env", f"PYTHONPATH={_SRC_IN_CONTAINER}",
            # Orchestrator registry DB on the host (sole writer: control plane).
            "--volume", f"{self._host_root}:{_ROOT_IN_CONTAINER}",
            "--env", f"BOT_BOTTLE_ROOT={_ROOT_IN_CONTAINER}",
            # Control-plane signing key (orchestrator: verifies tokens) + the
            # pre-minted `gateway` JWT (data-plane daemons: present it). gateway_init
            # scopes each to its process, so a compromised data-plane daemon never
            # sees the key and can't mint a `cli` token (issue #469 review).
            "--env", CONTROL_PLANE_TOKEN_ENV,
            "--env", CONTROL_AUTH_JWT_ENV,
            # Gateway daemons reach the orchestrator over loopback at its
            # fixed internal port (DEFAULT_PORT), independent of self.port.
            "--env", f"BOT_BOTTLE_ORCHESTRATOR_URL=http://127.0.0.1:{DEFAULT_PORT}",
            # Opt the orchestrator into gateway_init's supervise tree.
            "--env", f"BOT_BOTTLE_GATEWAY_DAEMONS={_INFRA_DAEMONS}",
            self.image,
        ], env={
            **os.environ,
            CONTROL_PLANE_TOKEN_ENV: _signing_key,
            CONTROL_AUTH_JWT_ENV: mint(ROLE_GATEWAY, _signing_key),
        })
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"infra container failed to start: {proc.stderr.strip()}"
            )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Ensure the infra container (control plane + gateway) is up; return
        the host control-plane URL. Idempotent — a healthy container on current
        source is left untouched. Raises `OrchestratorStartError` on timeout."""
        self._build_images()

        current_hash = source_hash(self._repo_root)
        if self.is_healthy() and self._infra_source_current(current_hash):
            return self.url

        log.info("starting infra container", context={"name": self._infra_name})
        self._run_infra_container(current_hash)

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.is_healthy():
                log.info("infra container healthy", context={"url": self.url})
                return self.url
            time.sleep(_HEALTH_POLL_SECONDS)
        raise OrchestratorStartError(
            f"infra container at {self.url} did not become healthy within {startup_timeout:g}s"
        )

    def stop(self) -> None:
        """Remove the infra container (idempotent)."""
        run_docker(["docker", "rm", "--force", self._infra_name])


__all__ = [
    "OrchestratorService",
    "OrchestratorStartError",
    "INFRA_NAME",
    "INFRA_IMAGE",
    "INFRA_SOURCE_HASH_LABEL",
    "ORCHESTRATOR_IMAGE",
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "source_hash",
]
