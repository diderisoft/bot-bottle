"""The docker orchestrator: one fixed-name container, on the `--internal`
control network plus a host-loopback publish for the CLI. Sole opener of
`bot-bottle.db` and sole holder of the signing key.

Docker 28+ doesn't publish ports for a container on only `--internal` networks,
so it starts on a dedicated publish network (masquerading off, external DNS
pointed at an unroutable resolver) and then joins the control network.
"""

from __future__ import annotations

import ipaddress
import os
import time
from pathlib import Path

from ...lib import log
from ... import resources
from .util import image_exists as docker_image_exists
from .util import run_docker
from .util import tag_image as docker_tag_image
from ...image_registry import RegistryError
from ...infra_image import pinned_infra_artifact, pull_pinned_image
from ...published_artifacts import FORMAT_OCI
from ...contract.hostctl import Settings
from ...contract.identity import ORCHESTRATOR_TOKEN_ENV
from ...paths import HOST_DB_FILENAME, bot_bottle_root
from ...control.gateway import GatewayError
from ...control.lifecycle import (
    DEFAULT_PORT,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    HostLink,
    Orchestrator,
    OrchestratorStartError,
    source_hash,
)

# The orchestrator container and the control network the gateway reaches it
# over. Agents are never on it. Same names on docker and macOS.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-orchestrator=1"
ORCHESTRATOR_NETWORK = "bot-bottle-orchestrator"
# Carries only the loopback port mapping; no NAT'd egress.
ORCHESTRATOR_PUBLISH_NETWORK = "bot-bottle-orchestrator-publish"
_NO_MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
# TEST-NET-1 as Docker DNS's upstream: external names don't resolve, container
# names do.
_NO_EXTERNAL_DNS = "192.0.2.1"
ORCHESTRATOR_DOCKERFILE = "Dockerfile.orchestrator"
# Container label recording the image's source hash.
ORCHESTRATOR_SOURCE_HASH_LABEL = "bot-bottle-orchestrator-source-hash"

# Host root mount (DB and state) in the orchestrator, the only container with
# the DB.
_ROOT_IN_CONTAINER = "/bot-bottle-root"
_DB_IN_CONTAINER = f"{_ROOT_IN_CONTAINER}/db/{HOST_DB_FILENAME}"

_HEALTH_POLL_SECONDS = 0.25


class DockerOrchestrator(Orchestrator):
    """The control plane as one fixed-name container, built from
    `Dockerfile.orchestrator`. `ensure_running` blocks until `/health` answers."""

    def __init__(
        self,
        image_ref: str | None = None,
        *,
        settings: Settings,
        name: str = ORCHESTRATOR_NAME,
        label: str = ORCHESTRATOR_LABEL,
        port: int = DEFAULT_PORT,
        control_network: str = ORCHESTRATOR_NETWORK,
        publish_network: str = ORCHESTRATOR_PUBLISH_NETWORK,
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
        self.image_ref = image_ref or settings.infra.orchestrator_image
        self._settings = settings
        self.name = name
        self.label = label
        self.port = port
        self.control_network = control_network
        self.publish_network = publish_network
        # Build context: the checkout root, or a copy staged from the wheel.
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        infra = settings.infra
        self._root_mount_source = str(
            root_mount_source or infra.docker_root_mount or host_root or bot_bottle_root()
        )
        self._client_network = client_network or infra.docker_client_network or None
        self._client_host = (
            client_host or infra.docker_host_address
            or (self.name if self._client_network else "127.0.0.1")
        )
        # A socket-shared CI runner reaches published ports over its Docker
        # network. Otherwise loopback.
        self._bind_host = bind_host or (
            "0.0.0.0"
            if not self._client_network and self._client_host != "127.0.0.1"
            else "127.0.0.1"
        )
        self._dockerfile = dockerfile

    def url(self) -> str:
        """Control-plane URL for this Docker client."""
        port = DEFAULT_PORT if self._client_network else self.port
        return f"http://{self._client_host}:{port}"

    def gateway_url(self) -> str:
        """The URL the gateway resolves policy against: the orchestrator by
        name on the control network."""
        return f"http://{self.name}:{DEFAULT_PORT}"

    def ensure_built(self) -> None:
        """Build the image, cache-aware. No-op without a dockerfile. `no_cache`
        forces a full rebuild."""
        if self._dockerfile is None:
            return
        # Pull the release-pinned image when it was built from this install's
        # code; build only when asked (`up --build`), else stop.
        try:
            pin = pinned_infra_artifact(
                "bot-bottle-orchestrator", FORMAT_OCI,
                local_build=self._settings.infra.infra_build_local)
        except RegistryError as e:
            raise OrchestratorStartError(str(e)) from e
        if pin is not None:
            try:
                self.image_ref = pull_pinned_image(
                    pin, local_ref=self.image_ref, image_exists=docker_image_exists,
                    tag_image=docker_tag_image, pull_argv=("docker", "pull"))
            except RegistryError as e:
                raise OrchestratorStartError(str(e)) from e
            return
        argv = ["docker", "build", "-t", self.image_ref,
                "-f", str(self._repo_root / self._dockerfile),
                str(self._repo_root)]
        if self._settings.operation.no_cache:
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

    def host_link(self) -> HostLink | None:
        """The publish network's bridge address on the host. Agents can route
        to it, so the host controller accepts only peers from this network."""
        proc = run_docker([
            "docker", "network", "inspect", "--format",
            "{{range .IPAM.Config}}{{.Gateway}} {{.Subnet}} {{end}}", self.publish_network,
        ])
        words = proc.stdout.split() if proc.returncode == 0 else []
        for gateway, subnet in zip(words[::2], words[1::2]):
            try:
                if ipaddress.ip_address(gateway) in ipaddress.IPv4Network(subnet):
                    return HostLink(address=gateway, network=subnet, backend="docker")
            except ValueError:
                continue
        return None

    def is_running(self) -> bool:
        proc = run_docker([
            "docker", "ps", "--filter", f"name=^/{self.name}$", "--format", "{{.Names}}",
        ])
        return self.name in proc.stdout.split()

    def _source_current(self, current_hash: str) -> bool:
        """True iff the running image matches current source."""
        if not self.is_running():
            return False
        proc = run_docker([
            "docker", "inspect", "--format",
            "{{ index .Config.Labels \"" + ORCHESTRATOR_SOURCE_HASH_LABEL + "\" }}",
            self.name,
        ])
        if proc.returncode != 0:
            return True  # can't compare: don't churn a working container
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

    def _ensure_publish_network(self) -> None:
        """Create the publish network with masquerading off; refuse an existing
        one with it on."""
        proc = run_docker([
            "docker", "network", "inspect", "--format",
            '{{index .Options "' + _NO_MASQUERADE + '"}}', self.publish_network,
        ])
        if proc.returncode == 0:
            if proc.stdout.strip() != "false":
                raise OrchestratorStartError(
                    f"network {self.publish_network} exists with IP masquerading on, "
                    "which would give the orchestrator outbound access; remove it "
                    f"(`docker network rm {self.publish_network}`) so it is recreated"
                )
            return
        proc = run_docker([
            "docker", "network", "create", "--opt", f"{_NO_MASQUERADE}=false",
            self.publish_network,
        ])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise OrchestratorStartError(
                f"orchestrator publish network {self.publish_network} failed to create: "
                f"{proc.stderr.strip()}"
            )

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> bool:
        """Ensure the container is up on current source and healthy. A healthy
        current one is left alone (keeping its egress tokens) and False
        returned; True when (re)started. Raises `OrchestratorStartError` on
        timeout."""
        current_hash = source_hash(self._repo_root)
        if self.is_healthy() and self._source_current(current_hash):
            return False
        log.info("starting orchestrator container", context={"name": self.name})
        self._run_container(current_hash)
        deadline = time.monotonic() + startup_timeout
        problem: str | None = "it was never probed"
        while time.monotonic() < deadline:
            problem = self.health_problem()
            if problem is None:
                log.info("orchestrator healthy", context={"url": self.url()})
                return True
            time.sleep(_HEALTH_POLL_SECONDS)
        raise OrchestratorStartError(
            f"orchestrator at {self.url()} did not become healthy "
            f"within {startup_timeout:g}s ({problem})"
        )

    def _run_container(self, current_hash: str) -> None:
        """Start the container on the publish network and join the control
        network. Clears a stale fixed-name container first."""
        self._ensure_control_network()
        self._ensure_publish_network()
        run_docker(["docker", "rm", "--force", self.name])
        # Raises rather than yield an empty key that would serve open.
        _signing_key = self.control_plane_key()
        host_env = self.host_control_env()
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", self.label,
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current_hash}",
            # Publish network first; the control network is joined after.
            "--network", self.publish_network,
            "--dns", _NO_EXTERNAL_DNS,
            # Host-side mapping (loopback by default) to the fixed in-container
            # port.
            "--publish", f"{self._bind_host}:{self.port}:{DEFAULT_PORT}",
            # Runs the package baked into the freshly built image, no source
            # mount. The root mount (a host path or a named volume) holds the
            # DB.
            "--volume", f"{self._root_mount_source}:{_ROOT_IN_CONTAINER}",
            # The signing key, as a bare `--env NAME`.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            # The host controller's URL and an `orchestrator` token, when it is
            # the one bringing this up.
            *[arg for name in sorted(host_env) for arg in ("--env", name)],
            self.image_ref,
            # Args to the `python3 -m bot_bottle.orchestrator` entrypoint.
            "--host", "0.0.0.0", "--port", str(DEFAULT_PORT),
            "--db", _DB_IN_CONTAINER,
        ], env={**os.environ, ORCHESTRATOR_TOKEN_ENV: _signing_key, **host_env})
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to start: {proc.stderr.strip()}"
            )
        proc = run_docker(["docker", "network", "connect", self.control_network, self.name])
        if proc.returncode != 0:
            raise OrchestratorStartError(
                f"orchestrator container failed to join control network "
                f"{self.control_network}: {proc.stderr.strip()}"
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
        """Remove the container. Idempotent."""
        run_docker(["docker", "rm", "--force", self.name])


__all__ = [
    "DockerOrchestrator",
    "ORCHESTRATOR_NAME",
    "ORCHESTRATOR_LABEL",
    "ORCHESTRATOR_NETWORK",
    "ORCHESTRATOR_PUBLISH_NETWORK",
    "ORCHESTRATOR_DOCKERFILE",
    "ORCHESTRATOR_SOURCE_HASH_LABEL",
]
