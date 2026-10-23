"""The docker orchestrator (control plane) as a single, fixed-name container.

`DockerOrchestrator` is the docker implementation of the backend-neutral
`Orchestrator` service. The lean control-plane container joins the `--internal`
control network (agents are never on it, so they have no L3 route to it) plus a
host-loopback publish for the CLI. It is the sole opener of `bot-bottle.db` and
the sole holder of the signing key.

The publish needs a second network: Docker doesn't publish ports for a
container whose only networks are `--internal` (Docker 28+), so the CLI could
never reach the control plane. The container is started on a dedicated publish
network with IP masquerading off (no NAT'd path to the internet; it can still
reach host services through the bridge address, as any bridge member can),
with external DNS pointed at an unroutable resolver so the publish network
can't be used to resolve or leak through DNS, and then joined to the control
network.
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

# The control plane's own container + the dedicated `--internal` control network
# the gateway reaches it over. The orchestrator + gateway join it, agents never
# do, so agents have no route to the control plane. Same name across docker + macOS.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"
ORCHESTRATOR_LABEL = "bot-bottle-orchestrator=1"
ORCHESTRATOR_NETWORK = "bot-bottle-orchestrator"
# Carries only the host-loopback port mapping; masquerading off, so no NAT'd
# egress.
ORCHESTRATOR_PUBLISH_NETWORK = "bot-bottle-orchestrator-publish"
_NO_MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
# Upstream for Docker's embedded DNS: TEST-NET-1, never routed, so external
# names don't resolve while container names on the control network still do.
_NO_EXTERNAL_DNS = "192.0.2.1"
ORCHESTRATOR_DOCKERFILE = "Dockerfile.orchestrator"
# Baked as a container label so `ensure_running` can detect whether the running
# orchestrator image was built from the current source.
ORCHESTRATOR_SOURCE_HASH_LABEL = "bot-bottle-orchestrator-source-hash"

# Bot-bottle host-root bind-mount (DB + state) inside the orchestrator. The
# control plane opens bot-bottle.db under here (passed as `--db`); it is the
# ONLY container with a handle on it.
_ROOT_IN_CONTAINER = "/bot-bottle-root"
_DB_IN_CONTAINER = f"{_ROOT_IN_CONTAINER}/db/{HOST_DB_FILENAME}"

_HEALTH_POLL_SECONDS = 0.25


class DockerOrchestrator(Orchestrator):
    """The control plane as a single fixed-name container. `ensure_built` builds
    it from `Dockerfile.orchestrator`; `ensure_running` starts it on the control
    network + host loopback and blocks until `/health` answers."""

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
        # Build context: the repo root in a checkout, a staged copy from the
        # installed wheel otherwise (bot_bottle.resources).
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
        pre-pulled image). `no_cache` forces a full rebuild."""
        if self._dockerfile is None:
            return
        # Pull the release-pinned image instead when it was built from exactly
        # this install's code; otherwise build it.
        pin = pinned_infra_artifact(
            "bot-bottle-orchestrator", FORMAT_OCI,
            local_build=self._settings.infra.infra_build_local)
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
        """The publish network's bridge address on the host. Only the
        orchestrator is on that network; agents can route to the address, so
        the host controller accepts only peers from this network."""
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

    def _ensure_publish_network(self) -> None:
        """Create the publish network with masquerading off, and refuse one
        that exists with it on: that would hand the control plane an outbound
        route."""
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
        """Ensure the control-plane container is up on current source; block
        until healthy. Idempotent — a healthy orchestrator on current source is
        left untouched (its in-memory egress tokens survive) and False is
        returned; True when it was (re)started. Raises
        `OrchestratorStartError` on startup timeout."""
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
        """Start the lean control-plane container on the publish network (which
        carries the host-loopback mapping for the CLI) and join it to the
        control network. Idempotent (clears a stale fixed-name container
        first)."""
        self._ensure_control_network()
        self._ensure_publish_network()
        run_docker(["docker", "rm", "--force", self.name])
        # The signing key comes through the shared provisioning contract,
        # which fail-closes rather than yield an empty key that would run OPEN.
        _signing_key = self.control_plane_key()
        host_env = self.host_control_env()
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", self.label,
            "--label", f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current_hash}",
            # The publish network: no other containers and no masquerading,
            # it only carries the loopback mapping below. The control network
            # is joined right after (agents are never on either, so they have
            # no route to the control plane: the L3 block, not just the JWT).
            "--network", self.publish_network,
            "--dns", _NO_EXTERNAL_DNS,
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
            # The signing key — held ONLY by the orchestrator (it verifies
            # tokens); the gateway gets the pre-minted `gateway` JWT, never the
            # key. Bare `--env NAME` keeps the value off argv.
            "--env", ORCHESTRATOR_TOKEN_ENV,
            # The host controller's URL and an `orchestrator` token, when it is
            # the one bringing this up (never its key).
            *[arg for name in sorted(host_env) for arg in ("--env", name)],
            self.image_ref,
            # Dockerfile.orchestrator ENTRYPOINT is `python3 -m bot_bottle.orchestrator`;
            # these are its args.
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
        """Remove the control-plane container (idempotent)."""
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
