"""`MacosOrchestrator`: the control plane as an Apple container on the
host-only control network only, with a container-only DB volume and the signing
key. The CLI and the gateway both reach it at its control-network address (no
container DNS).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ...environment import resource_prefix
from ...lib import log
from ... import resources
from ...contract.hostctl import Settings
from ...contract.identity import ORCHESTRATOR_TOKEN_ENV
from ...paths import REGISTRY_DB_FILENAME
from ...control.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    HostLink,
    Orchestrator,
    OrchestratorStartError,
    source_hash,
)
from . import util as container_mod
from ...image_registry import RegistryError
from ...infra_image import (
    BUILD_WITH_CONTAINER, local_build_tag, note_local_build, pinned_infra_artifact,
    pull_pinned_image, version_env_vars,
)
from ...published_artifacts import FORMAT_OCI
from .gateway import CONTROL_NETWORK

ORCHESTRATOR_NAME = resource_prefix() + "mac-orchestrator"
ORCHESTRATOR_LABEL = f"{ORCHESTRATOR_NAME}=1"
# Volume holding the orchestrator's `registry.db`, mounted only into it.
ORCHESTRATOR_DB_VOLUME = resource_prefix() + "mac-db"

# DB volume mount point; the DB is `<root>/db/<filename>`, passed as `--db`.
_DB_ROOT_IN_CONTAINER = "/var/lib/bot-bottle"
_DB_IN_CONTAINER = f"{_DB_ROOT_IN_CONTAINER}/db/{REGISTRY_DB_FILENAME}"
_SRC_IN_CONTAINER = "/bot-bottle-src"

_HEALTH_POLL_SECONDS = 0.25


class MacosOrchestrator(Orchestrator):
    """The control plane as an Apple container. `ensure_built` builds
    `Dockerfile.orchestrator`; `ensure_running` waits for `/health`."""

    def __init__(
        self,
        image_ref: str | None = None,
        *,
        settings: Settings,
        name: str = ORCHESTRATOR_NAME,
        label: str = ORCHESTRATOR_LABEL,
        port: int = DEFAULT_PORT,
        control_network: str = CONTROL_NETWORK,
        repo_root: Path | None = None,
        db_volume: str = ORCHESTRATOR_DB_VOLUME,
    ) -> None:
        self.image_ref = image_ref or settings.infra.orchestrator_image
        self._settings = settings
        self.name = name
        self.label = label
        self.port = port
        self.control_network = control_network
        # Build context and source mount: the checkout root, or a copy staged
        # from the wheel.
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        self._db_volume = db_volume

    def url(self) -> str:
        """The control-network address, or "" before it has one."""
        ip = container_mod.try_container_ipv4_on_network(self.name, self.control_network)
        return f"http://{ip}:{self.port}" if ip else ""

    def gateway_url(self) -> str:
        """Same as `url()`."""
        return self.url()

    def host_link(self) -> HostLink | None:
        """The host's vmnet gateway on the control network. Agents can route
        there too, so the host controller accepts only control-network peers."""
        found = container_mod.network_ipv4(self.control_network)
        if found is None:
            return None
        return HostLink(address=found[0], network=found[1], backend="macos-container")

    def is_running(self) -> bool:
        return container_mod.container_is_running(self.name)

    def _source_current(self, current_hash: str) -> bool:
        """True iff the running orchestrator loaded the current mounted source."""
        if not self.is_running():
            return False
        env = container_mod.container_env(self.name)
        if not env:
            return True  # can't compare: don't churn a working container
        return env.get("BOT_BOTTLE_SOURCE_HASH") == current_hash

    def ensure_built(self) -> None:
        """Build the image, or pull the release-pinned one when it was built
        from this install's code. The source is mounted, so code changes need
        no rebuild. Builds only when asked (`up --build`); otherwise stops."""
        try:
            pin = pinned_infra_artifact(
                "bot-bottle-orchestrator", FORMAT_OCI,
                local_build=self._settings.infra.infra_build_local,
                build_note=BUILD_WITH_CONTAINER)
        except RegistryError as e:
            raise OrchestratorStartError(str(e)) from e
        if pin is not None:
            try:
                self.image_ref = pull_pinned_image(
                    pin, local_ref=self.image_ref, image_exists=container_mod.image_exists,
                    tag_image=container_mod.tag_image,
                    pull_argv=("container", "image", "pull"))
            except RegistryError as e:
                raise OrchestratorStartError(str(e)) from e
            return
        # Reuse a local build of exactly this code; even a cached Apple build
        # costs ~10 s.
        built = local_build_tag("bot-bottle-orchestrator", self.image_ref)
        if not self._settings.operation.no_cache and container_mod.image_exists(built):
            self.image_ref = built
            return
        note_local_build()
        container_mod.build_image(
            self.image_ref, str(self._repo_root), dockerfile="Dockerfile.orchestrator",
            no_cache=self._settings.operation.no_cache,
            dns_override=self._settings.infra.macos_container_dns)
        container_mod.tag_image(self.image_ref, built)
        self.image_ref = built

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> bool:
        """Ensure the container is up on current source and healthy. A healthy
        current one is left alone (False); True when (re)started. Raises
        `OrchestratorStartError` on timeout. The control network must exist."""
        current_hash = source_hash(self._repo_root)
        if self._source_current(current_hash) and self.is_healthy():
            return False
        log.info("starting orchestrator container", context={"name": self.name})
        self._run_container(current_hash)
        self._wait_healthy(startup_timeout)
        return True

    def _run_container(self, current_hash: str) -> None:
        container_mod.force_remove_container(self.name)
        # Raises rather than yield an empty key that would serve open.
        _signing_key = self.control_plane_key()
        host_env = self.host_control_env()
        ver_env = version_env_vars()
        argv = [
            "container", "run", "--detach",
            "--name", self.name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", self.label,
            # Control network only.
            "--network", self.control_network,
            "--dns", container_mod.dns_server(self._settings.infra.macos_container_dns),
            # Container-only DB volume.
            "--volume", f"{self._db_volume}:{_DB_ROOT_IN_CONTAINER}",
            # Live source mount.
            "--mount",
            container_mod.bind_mount_spec(
                str(self._repo_root), _SRC_IN_CONTAINER, readonly=True),
            "--env", f"PYTHONPATH={_SRC_IN_CONTAINER}",
            # Recreate on a real code change.
            "--env", f"BOT_BOTTLE_SOURCE_HASH={current_hash}",
            # The signing key, as a bare `--env NAME`.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            # The host controller's URL and token, when it is the one starting
            # this.
            *[arg for name in sorted(host_env) for arg in ("--env", name)],
            # Version info for GET /version (computed host-side, passed into image).
            *[f"--env={k}={v}" for k, v in sorted(ver_env.items())],
            self.image_ref,
            # Args to the `-m bot_bottle.orchestrator` entrypoint.
            "--host", "0.0.0.0", "--port", str(self.port),
            "--db", _DB_IN_CONTAINER,
        ]
        result = container_mod.run_container_argv(
            argv, env={**os.environ, ORCHESTRATOR_TOKEN_ENV: _signing_key, **host_env})
        if result.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def _wait_healthy(self, startup_timeout: float) -> None:
        deadline = time.monotonic() + startup_timeout
        while True:
            problem = self.health_problem()
            if problem is None:
                log.info("orchestrator healthy", context={"url": self.url()})
                return
            if time.monotonic() >= deadline:
                raise OrchestratorStartError(
                    f"orchestrator did not become healthy within "
                    f"{startup_timeout:g}s ({problem}){_local_network_hint(problem)}"
                )
            time.sleep(_HEALTH_POLL_SECONDS)

    def stop(self) -> None:
        """Remove the container. Idempotent; the DB volume persists."""
        container_mod.force_remove_container(self.name)


def _local_network_hint(problem: str) -> str:
    """macOS Local Network privacy shows up as "No route to host"."""
    if "No route to host" not in problem:
        return ""
    return (f"; if `curl` reaches it, macOS Local Network privacy may be "
            f"blocking {sys.executable} — allow it under System Settings › "
            "Privacy & Security › Local Network")


def probe_orchestrator_url(port: int = DEFAULT_PORT) -> str:
    """The running orchestrator's URL, or "" if it isn't up or `container`
    isn't installed."""
    ip = container_mod.try_container_ipv4_on_network(ORCHESTRATOR_NAME, CONTROL_NETWORK)
    return f"http://{ip}:{port}" if ip else ""


__all__ = [
    "MacosOrchestrator",
    "ORCHESTRATOR_NAME",
    "ORCHESTRATOR_LABEL",
    "ORCHESTRATOR_DB_VOLUME",
    "probe_orchestrator_url",
]
