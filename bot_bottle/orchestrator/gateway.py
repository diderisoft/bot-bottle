"""The consolidated per-host gateway (PRD 0070).

The core consolidation win: **one** persistent gateway per host, shared by
every bottle, instead of a sidecar bundle per bottle. It's safe to share
because the attribution invariant (source IP + identity token, see
`registry`) lets the gateway attribute each request to the right bottle —
so per-bottle policy lives in one long-lived process keyed on who's calling.

`Gateway` is the backend-neutral lifecycle contract (mirrors `LaunchBroker`):
ensure the single instance is up, report it, tear it down. `DockerGateway`
is the docker implementation; a firecracker gateway VM slots in later.

The defining behaviour is **idempotent singleton**: `ensure_running` starts
the instance if absent and is a no-op if it's already up, so N bottle
launches never spawn N gateways.
"""

from __future__ import annotations

import abc
import os
from pathlib import Path

from ..docker_cmd import run_docker

GATEWAY_NAME = "bot-bottle-orch-gateway"
GATEWAY_LABEL = "bot-bottle-orch-gateway=1"
# The single user-defined network the gateway and every agent bottle share.
# Agents attach here with a pinned IP and reach the gateway's egress /
# git-http / supervise ports by its address — no host port publishing, and
# the source IP the gateway attributes by is the address on this network.
GATEWAY_NETWORK = "bot-bottle-gateway"

# mitmproxy's CA dir in the bundle. A persistent named volume here keeps the
# gateway's self-generated CA STABLE across container recreation — every agent
# installs this one CA to trust the shared gateway's TLS interception, so it
# must not rotate when the gateway restarts.
MITMPROXY_HOME = "/home/mitmproxy/.mitmproxy"
GATEWAY_CA_VOLUME = "bot-bottle-gateway-mitmproxy"
GATEWAY_CA_CERT = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"

# The real sidecar-bundle image + its Dockerfile. Kept as a local constant
# rather than imported from backend.docker.sidecar_bundle, which would drag
# the whole backend layer into the lean orchestrator (see #359); unify when
# that lands. Env override matches the backend's BOT_BOTTLE_SIDECAR_IMAGE.
GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_SIDECAR_IMAGE", "bot-bottle-sidecars:latest")
GATEWAY_DOCKERFILE = "Dockerfile.sidecars"
_REPO_ROOT = Path(__file__).resolve().parents[2]


class GatewayError(Exception):
    """The shared gateway failed to build/start/stop (non-zero `docker` exit)."""


class Gateway(abc.ABC):
    """Lifecycle of the single per-host gateway. Backend-neutral."""

    name: str

    def ensure_built(self) -> None:
        """Ensure the gateway's image / rootfs exists, building it if needed.
        Default: nothing to build (e.g. a stub or a pre-pulled image)."""
        return

    @abc.abstractmethod
    def ensure_running(self) -> None:
        """Start the gateway if it isn't already up. Idempotent: a no-op
        when it's already running (that's the whole point — one per host).
        Assumes the image exists — call `ensure_built()` first."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the gateway instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the gateway. Idempotent — absent is success."""


class DockerGateway(Gateway):
    """The consolidated gateway as a single, fixed-name Docker container.

    `image_ref` defaults to the real sidecar-bundle image; `ensure_built`
    builds it from `Dockerfile.sidecars` when it's missing. (Note: slice 5
    builds + launches the bundle container; wiring its per-bottle,
    source-IP-keyed config is a later slice — see PRD 0070.)"""

    def __init__(
        self,
        image_ref: str = GATEWAY_IMAGE,
        *,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        build_context: Path | None = None,
        dockerfile: str | None = GATEWAY_DOCKERFILE,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.network = network
        self._build_context = build_context or _REPO_ROOT
        self._dockerfile = dockerfile

    def image_exists(self) -> bool:
        return run_docker(["docker", "image", "inspect", self.image_ref]).returncode == 0

    def ensure_built(self) -> None:
        """Build the bundle image from its Dockerfile when it's missing.
        No-op when the image is already present, or when no dockerfile is
        configured (e.g. a pre-pulled image)."""
        if self._dockerfile is None or self.image_exists():
            return
        proc = run_docker([
            "docker", "build",
            "-t", self.image_ref,
            "-f", str(self._build_context / self._dockerfile),
            str(self._build_context),
        ])
        if proc.returncode != 0:
            raise GatewayError(f"gateway image build failed: {proc.stderr.strip()}")

    def is_running(self) -> bool:
        proc = run_docker([
            "docker", "ps",
            "--filter", f"name=^{self.name}$",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ])
        return self.name in proc.stdout.split()

    def _ensure_network(self) -> None:
        """Create the shared gateway network if it doesn't exist. Idempotent —
        a concurrent create loses harmlessly (the loser sees 'already exists').
        Docker picks the subnet; the launcher reads it back to allocate IPs."""
        if run_docker(["docker", "network", "inspect", self.network]).returncode == 0:
            return
        proc = run_docker(["docker", "network", "create", self.network])
        if proc.returncode != 0 and "already exists" not in proc.stderr:
            raise GatewayError(
                f"gateway network {self.network} failed to create: {proc.stderr.strip()}"
            )

    def ensure_running(self) -> None:
        if self.is_running():
            return
        self._ensure_network()
        # Clear any stale (stopped) container holding the fixed name, then
        # start fresh. `rm --force` on an absent name is a tolerated no-op.
        run_docker(["docker", "rm", "--force", self.name])
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", GATEWAY_LABEL,
            "--network", self.network,
            # Persist the self-generated CA so it survives restarts (agents
            # trust it) — see GATEWAY_CA_VOLUME.
            "--volume", f"{GATEWAY_CA_VOLUME}:{MITMPROXY_HOME}",
            self.image_ref,
        ])
        if proc.returncode != 0:
            raise GatewayError(f"gateway failed to start: {proc.stderr.strip()}")

    def ca_cert_pem(self) -> str:
        """The gateway's CA certificate (PEM) that agents install to trust its
        TLS interception. Read from the running container; raises if the
        gateway hasn't generated it yet (it's created on first mitmproxy
        start)."""
        proc = run_docker(["docker", "exec", self.name, "cat", GATEWAY_CA_CERT])
        if proc.returncode != 0 or not proc.stdout.strip():
            raise GatewayError(
                f"gateway CA cert not available: {proc.stderr.strip() or 'empty'}"
            )
        return proc.stdout

    def stop(self) -> None:
        proc = run_docker(["docker", "rm", "--force", self.name])
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise GatewayError(f"gateway failed to stop: {proc.stderr.strip()}")


__all__ = [
    "Gateway", "DockerGateway", "GatewayError",
    "GATEWAY_NAME", "GATEWAY_LABEL", "GATEWAY_IMAGE", "GATEWAY_NETWORK",
    "GATEWAY_CA_VOLUME", "GATEWAY_CA_CERT",
]
