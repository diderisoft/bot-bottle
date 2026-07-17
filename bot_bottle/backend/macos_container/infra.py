"""The per-host infra container for the macOS backend (PRD 0070).

A single persistent Apple container that runs BOTH the orchestrator control
plane and the gateway data plane — the macOS analogue of the Firecracker infra
VM (`backend/firecracker/infra_vm.py`), not the docker backend's two separate
containers.

Why one container, not two: Apple Containers are lightweight VMs, each with its
own kernel. The docker backend runs the orchestrator and gateway as two
containers safely because they share the host kernel, so their concurrent
writes to the one `bot-bottle.db` (the orchestrator's registry + the gateway
supervise daemon's queue) are serialized by coherent `fcntl` locks. Across two
*guest* kernels sharing a virtiofs-mounted DB those locks are not coherent, and
concurrent writers can corrupt the file. Firecracker solved this by putting
both services in one guest with the DB on a device only that guest mounts; this
does the same with Apple primitives.

Two consequences fall out of the single container, both simplifications:

- **No DNS dance.** The control plane and the gateway daemons reach each other
  over `127.0.0.1`, so nothing depends on Apple's (absent) container DNS and
  there is no orchestrator-before-gateway ordering to get right.
- **The DB is never host-shared.** It lives on a container-only volume, so no
  host process opens the live file. The host CLI reaches registry + supervise
  state through the control-plane HTTP surface (`cli/supervise.py` already uses
  `OrchestratorClient`), exactly as it does for firecracker.

The control-plane source is bind-mounted (like the docker orchestrator), so a
code change takes effect on the next launch without an image rebuild; the
gateway daemons are baked in the gateway image and rebuild through its own
digest check.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ... import log
from ...orchestrator.gateway import GATEWAY_CA_CERT
from ...orchestrator.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    OrchestratorStartError,
    source_hash,
)
from ...paths import (
    CONTROL_PLANE_TOKEN_ENV,
    HOST_DB_FILENAME,
    host_control_plane_token,
)
from . import util as container_mod
from .gateway import (
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_EGRESS_NETWORK,
    GATEWAY_IMAGE,
    GATEWAY_NETWORK,
    GatewayError,
    ensure_networks,
)

# The one per-host infra container: control plane + gateway data plane.
INFRA_NAME = "bot-bottle-mac-infra"
INFRA_LABEL = "bot-bottle-mac-infra=1"
# Container-only volume holding bot-bottle.db. No host bind-mount, so the DB is
# written by exactly one kernel (this container's). Survives recreation.
INFRA_DB_VOLUME = "bot-bottle-mac-db"

# BOT_BOTTLE_ROOT inside the container; host_db_path() resolves the DB to
# <root>/db/<filename> and the supervise daemon writes the same file.
_DB_ROOT_IN_CONTAINER = "/var/lib/bot-bottle"
_DB_PATH_IN_CONTAINER = f"{_DB_ROOT_IN_CONTAINER}/db/{HOST_DB_FILENAME}"
_SRC_IN_CONTAINER = "/bot-bottle-src"

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEALTH_POLL_SECONDS = 0.25
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0
_CA_POLL_SECONDS = 0.5

# The gateway subset the consolidated model runs (no per-bottle git:// daemon).
_GATEWAY_DAEMONS = "egress,git-http,supervise"


def _init_script(port: int) -> str:
    """PID-1 init: start the control plane and the gateway daemons, both in
    this container, reaching each other over loopback. Backgrounded so `wait`
    reaps as PID 1. No `set -e` — a transient daemon failure must not kill the
    whole container (gateway_init applies the same 'stay up' policy)."""
    return (
        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"mkdir -p $(dirname {_DB_PATH_IN_CONTAINER})\n"
        # Control plane, from the bind-mounted source (stdlib-only package).
        f"( cd {_SRC_IN_CONTAINER} && BOT_BOTTLE_ROOT={_DB_ROOT_IN_CONTAINER} "
        f"python3 -m bot_bottle.orchestrator --host 0.0.0.0 --port {port} "
        "--broker stub ) &\n"
        # Gateway data plane, multi-tenant against the local control plane.
        f"( cd /app && BOT_BOTTLE_GATEWAY_DAEMONS={_GATEWAY_DAEMONS} "
        f"BOT_BOTTLE_ORCHESTRATOR_URL=http://127.0.0.1:{port} "
        f"SUPERVISE_DB_PATH={_DB_PATH_IN_CONTAINER} python3 /app/gateway_init.py ) &\n"
        "while : ; do wait ; done\n"
    )


@dataclass(frozen=True)
class InfraEndpoint:
    """How to reach the running infra container. The control plane and the
    gateway are the same container, so one address serves both."""

    control_plane_url: str   # http://<infra ip>:8099 — host CLI + registration
    gateway_ip: str          # same container; agents' proxy / git-http / MCP target


class MacosInfraService:
    """Manages the single per-host infra container. Callers use
    `ensure_running()` (returns the endpoint) and `ca_cert_pem()`."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        image: str = GATEWAY_IMAGE,
        repo_root: Path = _REPO_ROOT,
        name: str = INFRA_NAME,
        db_volume: str = INFRA_DB_VOLUME,
    ) -> None:
        self.port = port
        self.network = network
        self.egress_network = egress_network
        self.image = image
        self._repo_root = repo_root
        self._name = name
        self._db_volume = db_volume

    def _resolve_url(self) -> str:
        """The control-plane URL, or "" while the container has no address."""
        ip = container_mod.try_container_ipv4_on_network(self._name, self.network)
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

    def _source_current(self, current_hash: str) -> bool:
        """True iff the running infra container was created from the current
        bind-mounted control-plane source. The control-plane process loads that
        code at startup and won't reload it, so a stale container keeps serving
        OLD code."""
        if not container_mod.container_is_running(self._name):
            return False
        env = container_mod.container_env(self._name)
        if not env:
            return True  # can't compare → don't churn a working container
        return env.get("BOT_BOTTLE_SOURCE_HASH") == current_hash

    def _running_healthy_endpoint(self, current_hash: str) -> InfraEndpoint | None:
        """The endpoint if the running container is BOTH source-current and
        answering /health, else None (→ recreate). Health, not just the source
        label, is what lets a wedged-but-current container self-heal instead of
        being polled to death forever."""
        if not self._source_current(current_hash):
            return None
        url = self._resolve_url()
        if url and self.is_healthy(url):
            return InfraEndpoint(control_plane_url=url, gateway_ip=_ip_of(url))
        return None

    def ensure_built(self) -> None:
        """Ensure the gateway data-plane image exists. The control-plane source
        is bind-mounted, not baked, so only the gateway image needs building."""
        container_mod.build_image(
            self.image, str(self._repo_root), dockerfile="Dockerfile.gateway",
        )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> InfraEndpoint:
        """Ensure the single infra container is up; return how to reach it.
        Idempotent per-host singleton — a healthy container on current source
        is left untouched, so N launches share the one control plane + gateway.
        Raises `OrchestratorStartError` on startup timeout."""
        current_hash = source_hash(self._repo_root)
        endpoint = self._running_healthy_endpoint(current_hash)
        if endpoint is not None:
            return endpoint
        self.ensure_built()
        log.info("starting infra container", context={"name": self._name})
        self._run_container(current_hash)
        return self._wait_healthy(startup_timeout)

    def _run_container(self, current_hash: str) -> None:
        ensure_networks(self.network, self.egress_network)
        container_mod.force_remove_container(self._name)
        argv = [
            "container", "run", "--detach",
            "--name", self._name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", INFRA_LABEL,
            # NAT network FIRST so the gateway's egress has a default route;
            # the host-only network is where agents (and the host CLI) reach it.
            "--network", self.egress_network,
            "--network", self.network,
            "--dns", container_mod.dns_server(),
            # Container-only DB volume: one kernel writes bot-bottle.db, never
            # shared with the host or another guest.
            "--volume", f"{self._db_volume}:{_DB_ROOT_IN_CONTAINER}",
            # Bind-mount the control-plane source (read-only); a code change
            # takes effect on relaunch with no image rebuild.
            "--mount",
            container_mod.bind_mount_spec(
                str(self._repo_root), _SRC_IN_CONTAINER, readonly=True),
            # Baked onto the container so `_source_current` can detect a real
            # control-plane code change and recreate.
            "--env", f"BOT_BOTTLE_SOURCE_HASH={current_hash}",
            # The control-plane secret, for BOTH the control plane (to require
            # it) and the gateway's PolicyResolver (to present it) — they share
            # this one container. Bare `--env NAME` inherits the value from the
            # run process below, so the secret never lands on argv or in
            # `container inspect`'s command line. The agent runs in a SEPARATE
            # container that is never given this var, which is the whole point.
            "--env", CONTROL_PLANE_TOKEN_ENV,
            "--entrypoint", "sh",
            self.image,
            "-c", _init_script(self.port),
        ]
        run_env = {**os.environ, CONTROL_PLANE_TOKEN_ENV: host_control_plane_token()}
        result = container_mod.run_container_argv(argv, env=run_env)
        if result.returncode != 0:
            raise OrchestratorStartError(
                f"infra container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def _wait_healthy(self, startup_timeout: float) -> InfraEndpoint:
        deadline = time.monotonic() + startup_timeout
        while True:
            url = self._resolve_url()
            if url and self.is_healthy(url):
                log.info("infra container healthy", context={"url": url})
                return InfraEndpoint(control_plane_url=url, gateway_ip=_ip_of(url))
            if time.monotonic() >= deadline:
                raise OrchestratorStartError(
                    f"infra container did not become healthy within "
                    f"{startup_timeout:g}s"
                )
            time.sleep(_HEALTH_POLL_SECONDS)

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception. Read out of the container (the CA lives on a
        container-internal path, not a host mount); polls because mitmproxy
        writes it a beat after start."""
        deadline = time.monotonic() + timeout
        while True:
            result = container_mod.run_container_argv(
                ["container", "exec", self._name, "cat", GATEWAY_CA_CERT])
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
            if time.monotonic() >= deadline:
                raise GatewayError(
                    f"gateway CA not available in {self._name} after {timeout:g}s: "
                    f"{(result.stderr or '').strip() or 'empty'}"
                )
            time.sleep(_CA_POLL_SECONDS)

    def stop(self) -> None:
        """Remove the infra container (idempotent). The DB volume persists."""
        container_mod.force_remove_container(self._name)


def _ip_of(url: str) -> str:
    """The host from an http://host:port URL."""
    return url.split("://", 1)[-1].rsplit(":", 1)[0]


def probe_control_plane_url(port: int = DEFAULT_PORT) -> str:
    """The running infra container's control-plane URL, or "" if it isn't up.
    Used by host-side control-plane discovery (`discover_orchestrator_url`);
    safe to call on any host — returns "" when the container or the `container`
    CLI isn't present."""
    ip = container_mod.try_container_ipv4_on_network(INFRA_NAME, GATEWAY_NETWORK)
    return f"http://{ip}:{port}" if ip else ""


__all__ = [
    "MacosInfraService",
    "InfraEndpoint",
    "OrchestratorStartError",
    "GatewayError",
    "INFRA_NAME",
    "INFRA_DB_VOLUME",
]
