"""The macOS orchestrator (control plane) as an Apple container (PRD 0070).

`MacosOrchestrator` is the Apple-Container implementation of the backend-neutral
`Orchestrator` service. The lean control-plane container joins the host-only
control network only (agents are never on it), mounts a container-only DB volume
(exactly one kernel writes `bot-bottle.db`), and holds the signing key (#469).
The host CLI and the gateway both reach it at its control-network address —
Apple has no container DNS, so there's a single resolved URL, not docker's
loopback-vs-name split.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ... import log
from ... import resources
from ...paths import ORCHESTRATOR_TOKEN_ENV
from ...orchestrator.lifecycle import (
    DEFAULT_HEALTH_TIMEOUT_SECONDS,
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    Orchestrator,
    OrchestratorStartError,
    source_hash,
)
from . import util as container_mod
from .gateway import CONTROL_NETWORK

ORCHESTRATOR_IMAGE = os.environ.get(
    "BOT_BOTTLE_ORCHESTRATOR_IMAGE", "bot-bottle-orchestrator:latest"
)
ORCHESTRATOR_NAME = "bot-bottle-mac-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-mac-orchestrator=1"
# Container-only volume holding bot-bottle.db, mounted ONLY into the
# orchestrator. One kernel writes it (never host-shared or cross-guest).
ORCHESTRATOR_DB_VOLUME = "bot-bottle-mac-db"

# BOT_BOTTLE_ROOT inside the orchestrator; host_db_path() resolves the DB to
# <root>/db/<filename>.
_DB_ROOT_IN_CONTAINER = "/var/lib/bot-bottle"
_SRC_IN_CONTAINER = "/bot-bottle-src"

_HEALTH_POLL_SECONDS = 0.25


class MacosOrchestrator(Orchestrator):
    """The control plane as an Apple container on the host-only control network.
    `ensure_built` builds `Dockerfile.orchestrator`; `ensure_running` starts it
    and blocks until `/health` answers at its control-network address."""

    def __init__(
        self,
        image_ref: str = ORCHESTRATOR_IMAGE,
        *,
        name: str = ORCHESTRATOR_NAME,
        label: str = ORCHESTRATOR_LABEL,
        port: int = DEFAULT_PORT,
        control_network: str = CONTROL_NETWORK,
        repo_root: Path | None = None,
        db_volume: str = ORCHESTRATOR_DB_VOLUME,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.label = label
        self.port = port
        self.control_network = control_network
        # Build context / bind-mount source: the repo root in a checkout, a
        # staged copy from the installed wheel otherwise (bot_bottle.resources).
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        self._db_volume = db_volume

    def url(self) -> str:
        """The orchestrator's control-network address (host CLI + registration),
        or "" while it has no address yet. Apple has no container DNS, so this is
        also what the gateway resolves against (`gateway_url`)."""
        ip = container_mod.try_container_ipv4_on_network(self.name, self.control_network)
        return f"http://{ip}:{self.port}" if ip else ""

    def gateway_url(self) -> str:
        """Same address the host uses — the gateway reaches the orchestrator by
        control-network IP (Apple has no container DNS)."""
        return self.url()

    def is_healthy(self, *, timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS) -> bool:
        url = self.url()
        if not url:
            return False
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def is_running(self) -> bool:
        return container_mod.container_is_running(self.name)

    def _source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator was created from the current
        bind-mounted control-plane source (it loads that code at startup and
        won't reload it)."""
        if not self.is_running():
            return False
        env = container_mod.container_env(self.name)
        if not env:
            return True  # can't compare → don't churn a working container
        return env.get("BOT_BOTTLE_SOURCE_HASH") == current_hash

    def ensure_built(self) -> None:
        """Build the control-plane image. The source is bind-mounted so a code
        change takes effect without a rebuild; the image still carries the
        package for its entrypoint."""
        container_mod.build_image(
            self.image_ref, str(self._repo_root), dockerfile="Dockerfile.orchestrator")

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """Ensure the control-plane container is up on current source; block
        until healthy. Idempotent — a healthy orchestrator on current source is
        left untouched. Raises `OrchestratorStartError` on startup timeout.
        The control network must already exist (the composer ensures it)."""
        current_hash = source_hash(self._repo_root)
        if self._source_current(current_hash) and self.is_healthy():
            return
        log.info("starting orchestrator container", context={"name": self.name})
        self._run_container(current_hash)
        self._wait_healthy(startup_timeout)

    def _run_container(self, current_hash: str) -> None:
        container_mod.force_remove_container(self.name)
        # The signing key comes through the shared provisioning contract (#476),
        # which fail-closes rather than yield an empty key that would run OPEN.
        _signing_key = self.control_plane_key()
        argv = [
            "container", "run", "--detach",
            "--name", self.name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", self.label,
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
            self.image_ref,
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

    def _wait_healthy(self, startup_timeout: float) -> None:
        deadline = time.monotonic() + startup_timeout
        while True:
            if self.is_healthy():
                log.info("orchestrator healthy", context={"url": self.url()})
                return
            if time.monotonic() >= deadline:
                raise OrchestratorStartError(
                    f"orchestrator did not become healthy within "
                    f"{startup_timeout:g}s"
                )
            time.sleep(_HEALTH_POLL_SECONDS)

    def stop(self) -> None:
        """Remove the control-plane container (idempotent). The DB volume
        persists."""
        container_mod.force_remove_container(self.name)


def probe_orchestrator_url(port: int = DEFAULT_PORT) -> str:
    """The running orchestrator's control-plane URL, or "" if it isn't up. Used
    by host-side control-plane discovery; safe on any host (returns "" when the
    container or the `container` CLI isn't present)."""
    ip = container_mod.try_container_ipv4_on_network(ORCHESTRATOR_NAME, CONTROL_NETWORK)
    return f"http://{ip}:{port}" if ip else ""


__all__ = [
    "MacosOrchestrator",
    "ORCHESTRATOR_NAME",
    "ORCHESTRATOR_LABEL",
    "ORCHESTRATOR_IMAGE",
    "ORCHESTRATOR_DB_VOLUME",
    "probe_orchestrator_url",
]
