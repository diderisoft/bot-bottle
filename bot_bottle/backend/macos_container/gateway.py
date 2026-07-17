"""The consolidated per-host gateway as an Apple container (PRD 0070).

The macOS counterpart of `orchestrator.gateway.DockerGateway`: one persistent
gateway per host, shared by every bottle, attributing each request to a bottle
by its source IP on the shared host-only network.

Two Apple Container 1.0.0 constraints shape this and make it *not* a
transliteration of the docker gateway:

- **No container DNS.** Containers cannot resolve each other by name (the
  host-only network's resolver refuses the query), so the gateway reaches the
  control plane by **IP**, not by name as the docker gateway does. The
  orchestrator must therefore be started *before* the gateway — see
  `orchestrator_service`.
- **Networks are fixed at `container run`.** There is no `network connect`,
  so a network cannot be attached to a running container. The gateway must sit
  on one shared, up-front network for the lifetime of the process; per-bottle
  networks would mean restarting the gateway on every launch, which defeats the
  consolidation.

The gateway is dual-homed, **NAT network first**: Apple Container makes the
first `--network` the default route, so the egress network must lead or the
gateway has no route to the internet.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ...orchestrator.gateway import (
    GATEWAY_CA_CERT,
    GATEWAY_DOCKERFILE,
    MITMPROXY_HOME,
    Gateway,
    GatewayError,
)
from ...paths import host_db_path
from ...supervise import DB_PATH_IN_CONTAINER
from . import util as container_mod

# Distinct from the docker gateway's names so both backends' gateways can
# coexist on one host (a macOS host can run the docker backend too).
GATEWAY_NAME = "bot-bottle-mac-gateway"
# The shared host-only network the gateway and every agent bottle sit on. The
# agent's address here is the attribution key.
GATEWAY_NETWORK = "bot-bottle-mac-gateway"
# The NAT network that gives the gateway (and only the gateway) a route out.
GATEWAY_EGRESS_NETWORK = "bot-bottle-mac-egress"

GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SUPERVISE_DB_DIR_IN_CONTAINER = os.path.dirname(DB_PATH_IN_CONTAINER)

# mitmproxy writes its CA a beat after start; reads poll rather than assume.
_CA_POLL_SECONDS = 0.5
DEFAULT_CA_TIMEOUT_SECONDS = 30.0


def gateway_ca_dir() -> Path:
    """Host dir bind-mounted as mitmproxy's home, keeping the gateway's
    self-generated CA **stable across container recreation** — every agent
    installs this one CA to trust the shared gateway's TLS interception, so it
    must not rotate when the gateway restarts.

    The docker gateway uses a named volume for this; a plain host dir is the
    same guarantee with fewer moving parts, and it lets `ca_cert_pem` read the
    PEM straight off the host instead of shelling into the container."""
    path = host_db_path().parent / "mac-gateway-ca"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_networks(
    network: str = GATEWAY_NETWORK, egress_network: str = GATEWAY_EGRESS_NETWORK,
) -> None:
    """Create the shared host-only network + the gateway's NAT network.
    Idempotent — `create_network` tolerates 'already exists'.

    Module-level rather than a gateway method because the **orchestrator**
    needs the shared network too, and it starts first (Apple has no container
    DNS, so the gateway must be handed the control plane's IP). Both callers
    ensure the networks; whoever runs first wins."""
    container_mod.create_network(egress_network)
    container_mod.create_network(network, internal=True)


def _host_db_dir() -> str:
    db_dir = host_db_path().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir)


def _mount(source: str, target: str, *, readonly: bool = False) -> str:
    spec = f"type=bind,source={source},target={target}"
    if readonly:
        spec += ",readonly"
    return spec


class AppleGateway(Gateway):
    """The consolidated gateway as a single, fixed-name Apple container."""

    def __init__(
        self,
        image_ref: str = GATEWAY_IMAGE,
        *,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        orchestrator_url: str = "",
        build_context: Path | None = None,
        dockerfile: str | None = GATEWAY_DOCKERFILE,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.network = network
        self.egress_network = egress_network
        # Reached by IP (no container DNS on Apple) — the caller resolves the
        # orchestrator's address before constructing this. Empty → single-tenant.
        self._orchestrator_url = orchestrator_url
        self._build_context = build_context or _REPO_ROOT
        self._dockerfile = dockerfile

    def ensure_built(self) -> None:
        """Build the gateway data-plane image from its Dockerfile. Builds every
        time (cache-aware, so it's cheap when nothing changed): a stale image
        silently runs the OLD single-tenant daemons. Mirrors `DockerGateway`."""
        if self._dockerfile is None:
            return
        container_mod.build_image(
            self.image_ref, str(self._build_context), dockerfile=self._dockerfile,
        )

    def is_running(self) -> bool:
        return container_mod.container_is_running(self.name)

    def _running_image_is_current(self) -> bool:
        """True iff the running gateway was created from the *current*
        `image_ref`. `ensure_built` rebuilding the image is not enough on its
        own — the running container still holds the OLD image, so this
        mismatch check is what makes a rebuild take effect."""
        running = container_mod.container_image_digest(self.name)
        current = container_mod.image_digest(self.image_ref)
        if not running or not current:
            return True  # can't compare → don't churn a working container
        return running == current

    def _running_control_plane_is_current(self) -> bool:
        """True iff the running gateway points at the control plane we would
        pass today.

        Docker gets this for free — it hands the gateway a container *name*,
        which survives the orchestrator being recreated. Apple has no container
        DNS, so the URL is an **IP baked into the gateway's env at run time**,
        and a recreated orchestrator can come back on a different DHCP address.
        Without this check the gateway would keep pointing at the old address
        and every `/resolve` would fail — denying egress for *every* bottle on
        the host until something else happened to recreate the gateway."""
        if not self._orchestrator_url:
            return True
        env = container_mod.container_env(self.name)
        if not env:
            return True  # can't compare → don't churn a working container
        return env.get("BOT_BOTTLE_ORCHESTRATOR_URL") == self._orchestrator_url

    def ensure_running(self) -> None:
        if (self.is_running()
                and self._running_image_is_current()
                and self._running_control_plane_is_current()):
            return
        ensure_networks(self.network, self.egress_network)
        container_mod.force_remove_container(self.name)
        argv = [
            "container", "run", "--detach",
            "--name", self.name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", "bot-bottle-mac-gateway=1",
            # NAT network FIRST: Apple Container takes the first --network as
            # the default route, so this ordering is what gives the gateway a
            # route out. Reversing it silently blackholes egress.
            "--network", self.egress_network,
            "--network", self.network,
            # The NAT gateway routes but does not resolve, so DNS is explicit.
            "--dns", container_mod.dns_server(),
            "--mount", _mount(str(gateway_ca_dir()), MITMPROXY_HOME),
            "--mount", _mount(_host_db_dir(), _SUPERVISE_DB_DIR_IN_CONTAINER),
            "--env", f"SUPERVISE_DB_PATH={DB_PATH_IN_CONTAINER}",
        ]
        if self._orchestrator_url:
            # Makes the data plane multi-tenant: each request resolves
            # source-IP → policy against the control plane.
            argv += ["--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={self._orchestrator_url}"]
        argv.append(self.image_ref)
        result = container_mod.run_container_argv(argv)
        if result.returncode != 0:
            raise GatewayError(
                f"gateway failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )

    def ip_on_shared_network(self) -> str:
        """The gateway's address on the shared host-only network — what agents
        point their proxy / git-http / supervise URLs at."""
        return container_mod.container_ipv4_on_network(self.name, self.network)

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's CA certificate (PEM) that agents install to trust its
        TLS interception. Polls: mitmproxy generates it a moment after start.
        Read from the host bind-mount, so no exec into the container."""
        ca_path = gateway_ca_dir() / os.path.basename(GATEWAY_CA_CERT)
        deadline = time.monotonic() + timeout
        while True:
            try:
                pem = ca_path.read_text()
                if pem.strip():
                    return pem
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise GatewayError(
                    f"gateway CA cert not available at {ca_path} after {timeout:g}s"
                )
            time.sleep(_CA_POLL_SECONDS)

    def stop(self) -> None:
        container_mod.force_remove_container(self.name)


__all__ = [
    "AppleGateway",
    "GatewayError",
    "GATEWAY_NAME",
    "GATEWAY_NETWORK",
    "GATEWAY_EGRESS_NETWORK",
    "GATEWAY_IMAGE",
    "gateway_ca_dir",
]
