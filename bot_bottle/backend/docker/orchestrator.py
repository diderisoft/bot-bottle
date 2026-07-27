"""The docker orchestrator (control plane) as a single, fixed-name container
(PRD 0070).

`DockerOrchestrator` is the docker implementation of the backend-neutral
`Orchestrator` service. The lean control-plane container joins the `--internal`
control network only (agents are never on it, so they have no L3 route to it)
plus a host-loopback publish for the CLI. It is the sole opener of
`bot-bottle.db` and the sole holder of the signing key (#469).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ... import log
from ... import resources
from .util import run_docker
from ...paths import (
    ORCHESTRATOR_TOKEN_ENV,
    bot_bottle_root,
)
from ...gateway import GatewayError
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    Orchestrator,
    OrchestratorStartError,
    source_hash,
)

# The control plane's own container + the dedicated `--internal` control network
# the gateway reaches it over. The orchestrator + gateway join it, agents never
# do, so agents have no route to the control plane (PRD 0070 "Separating the
# planes"). Same name across docker + macOS.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-orchestrator=1"
ORCHESTRATOR_NETWORK = "bot-bottle-orchestrator"
ORCHESTRATOR_IMAGE = os.environ.get(
    "BOT_BOTTLE_ORCHESTRATOR_IMAGE", "bot-bottle-orchestrator:latest"
)
ORCHESTRATOR_DOCKERFILE = "Dockerfile.orchestrator"
# Baked as a container label so `ensure_running` can detect whether the running
# orchestrator image was built from the current source.
ORCHESTRATOR_SOURCE_HASH_LABEL = "bot-bottle-orchestrator-source-hash"

# Bot-bottle host-root bind-mount (DB + state) inside the orchestrator. The
# control plane opens bot-bottle.db under here (via BOT_BOTTLE_ROOT ->
# host_db_path()); it is the ONLY container with a handle on it (issue #469).
_ROOT_IN_CONTAINER = "/bot-bottle-root"

_HEALTH_POLL_SECONDS = 0.25


class DockerOrchestrator(Orchestrator):
    """The control plane as a single fixed-name container. `ensure_built` builds
    it from `Dockerfile.orchestrator`; `ensure_running` starts it on the control
    network + host loopback and blocks until `/health` answers."""

    def __init__(
        self,
        image_ref: str = ORCHESTRATOR_IMAGE,
        *,
        name: str = ORCHESTRATOR_NAME,
        label: str = ORCHESTRATOR_LABEL,
        port: int = DEFAULT_PORT,
        control_network: str = ORCHESTRATOR_NETWORK,
        repo_root: Path | None = None,
        host_root: Path | None = None,
        root_mount_source: str | Path | None = None,
        client_host: str | None = None,
        client_network: str | None = None,
        bind_host: str | None = None,
        dockerfile: str | None = ORCHESTRATOR_DOCKERFILE,
    ) -> None:
        if host_root is not None and root_mount_source is not None:
            raise ValueError("pass host_root or root_mount_source, not both")
        self.image_ref = image_ref
        self.name = name
        self.label = label
        self.port = port
        self.control_network = control_network
        # Build context: the repo root in a checkout, a staged copy from the
        # installed wheel otherwise (bot_bottle.resources).
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        configured_root = os.environ.get("BOT_BOTTLE_DOCKER_ROOT_MOUNT", "").strip()
        self._root_mount_source = str(
            root_mount_source or configured_root or host_root or bot_bottle_root()
        )
        configured_network = os.environ.get(
            "BOT_BOTTLE_DOCKER_CLIENT_NETWORK", ""
        ).strip()
        self._client_network = client_network or configured_network or None
        configured_host = os.environ.get(
            "BOT_BOTTLE_DOCKER_HOST_ADDRESS", ""
        ).strip()
        self._client_host = (
            client_host or configured_host
            or (self.name if self._client_network else "127.0.0.1")
        )
        # A socket-shared CI runner reaches published ports through its Docker
        # network rather than its own loopback. Production stays bound to host
        # loopback unless a caller explicitly selects another client.
        self._bind_host = bind_host or (
            "0.0.0.0"
            if not self._client_network and self._client_host != "127.0.0.1"
            else "127.0.0.1"
        )
        self._dockerfile = dockerfile

    def url(self) -> str:
        """Control-plane URL reachable by this Docker client."""
        port = DEFAULT_PORT if self._client_network else self.port
        return f"http://{self._client_host}:{port}"

    def gateway_url(self) -> str:
        """The URL the gateway's data plane resolves policy against — the
        orchestrator container by name on the control network (docker DNS,
        container↔container, no host firewall)."""
        return f"http://{self.name}:{DEFAULT_PORT}"

    def ensure_built(self) -> None:
        """Build the control-plane image from its Dockerfile, cache-aware (a
        no-op when nothing changed). No-op when no dockerfile is configured (a
        pre-pulled image). BOT_BOTTLE_NO_CACHE forces a full rebuild."""
        if self._dockerfile is None:
            proc = run_docker(["docker", "pull", self.image_ref])
            if proc.returncode != 0:
                raise GatewayError(
                    f"orchestrator image pull failed: {proc.stderr.strip()}")
            return
        argv = ["docker", "build", "-t", self.image_ref,
                "-f", str(self._repo_root / self._dockerfile),
                str(self._repo_root)]
        if os.environ.get("BOT_BOTTLE_NO_CACHE"):
            argv.insert(2, "--no-cache")
        for name, value in resources.image_build_args(
            self._dockerfile,
            context=self._repo_root,
        ).items():
            argv[-1:-1] = ["--build-arg", f"{name}={value}"]
        proc = run_docker(argv)
        if proc.returncode != 0:
            raise GatewayError(
                f"{self._dockerfile} build failed: {proc.stderr.strip()}")

    def is_running(self) -> bool:
        proc = run_docker([
            "docker", "ps", "--filter", f"name=^/{self.name}$", "--format", "{{.Names}}",
        ])
        return self.name in proc.stdout.split()

    def _source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator image matches current source."""
        if not self.is_running():
            return False
        proc = run_docker([
            "docker", "inspect", "--format",
            "{{ index .Config.Labels \"" + ORCHESTRATOR_SOURCE_HASH_LABEL + "\" }}",
            self.name,
        ])
        if proc.returncode != 0:
            return True  # can't compare → don't churn a working container
        return proc.stdout.strip() == current_hash

    def _ensure_control_network(self) -> None:
        if run_docker(["docker", "network", "inspect", self.control_network]).returncode == 0:
            return
        proc = run_docker(["docker", "network", "create", "--internal", self.control_network])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"control network {self.control_network} failed to create: "
                f"{proc.stderr.strip()}"
            )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """Ensure the control-plane container is up on current source; block
        until healthy. Idempotent — a healthy orchestrator on current source is
        left untouched (its in-memory egress tokens survive). Raises
        `OrchestratorStartError` on startup timeout."""
        current_hash = source_hash(self._repo_root)
        if self.is_healthy() and self._source_current(current_hash):
            return
        log.info("starting orchestrator container", context={"name": self.name})
        self._run_container(current_hash)
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.is_healthy():
                log.info("orchestrator healthy", context={"url": self.url()})
                return
            time.sleep(_HEALTH_POLL_SECONDS)
        raise OrchestratorStartError(
            f"orchestrator at {self.url()} did not become healthy "
            f"within {startup_timeout:g}s"
        )

    def _run_container(self, current_hash: str) -> None:
        """Start the lean control-plane container on the control network only,
        published to host loopback for the CLI. Idempotent (clears a stale
        fixed-name container first)."""
        self._ensure_control_network()
        run_docker(["docker", "rm", "--force", self.name])
        # The signing key comes through the shared provisioning contract (#476),
        # which fail-closes rather than yield an empty key that would run OPEN.
        _signing_key = self.control_plane_key()
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", self.label,
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current_hash}",
            # Control network only — agents are never on it, so they have no
            # route to the control plane (the L3 block, not just the JWT).
            "--network", self.control_network,
            # Host CLI reaches the control plane here (loopback by default).
            # Socket-shared CI joins the container directly to the job network;
            # the host-side mapping remains loopback-only in that topology. The
            # orchestrator listens on the fixed DEFAULT_PORT inside the
            # container; self.port is the host-side published port.
            "--publish", f"{self._bind_host}:{self.port}:{DEFAULT_PORT}",
            # The image was rebuilt from `_repo_root` immediately before this
            # launch. Running its baked package avoids a host-path bind mount,
            # which is both more production-like and works with socket-shared
            # CI where the daemon cannot see the job container's workspace.
            # Orchestrator registry DB on the host (sole writer: control plane).
            # `root_mount_source` may be a host path or a named Docker volume.
            "--volume", f"{self._root_mount_source}:{_ROOT_IN_CONTAINER}",
            "--env", f"BOT_BOTTLE_ROOT={_ROOT_IN_CONTAINER}",
            # The signing key — held ONLY by the orchestrator (it verifies
            # tokens); the gateway gets the pre-minted `gateway` JWT, never the
            # key (issue #469). Bare `--env NAME` keeps the value off argv.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            self.image_ref,
            # Dockerfile.orchestrator ENTRYPOINT is `python3 -m bot_bottle.orchestrator`;
            # these are its args.
            "--host", "0.0.0.0", "--port", str(DEFAULT_PORT), "--broker", "stub",
        ], env={**os.environ, ORCHESTRATOR_TOKEN_ENV: _signing_key})
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: {proc.stderr.strip()}"
            )
        if self._client_network:
            proc = run_docker([
                "docker", "network", "connect", self._client_network, self.name,
            ])
            if proc.returncode != 0:
                raise OrchestratorStartError(
                    f"orchestrator container failed to join client network "
                    f"{self._client_network}: {proc.stderr.strip()}"
                )

    def stop(self) -> None:
        """Remove the control-plane container (idempotent)."""
        run_docker(["docker", "rm", "--force", self.name])


__all__ = [
    "DockerOrchestrator",
    "ORCHESTRATOR_NAME",
    "ORCHESTRATOR_LABEL",
    "ORCHESTRATOR_NETWORK",
    "ORCHESTRATOR_IMAGE",
    "ORCHESTRATOR_DOCKERFILE",
    "ORCHESTRATOR_SOURCE_HASH_LABEL",
]
