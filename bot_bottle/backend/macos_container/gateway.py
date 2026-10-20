"""The macOS gateway data plane as an Apple container (PRD 0070).

`MacosGateway` is the Apple-Container implementation of the backend-neutral
`Gateway` service. It runs the gateway daemons (egress / git-http / supervise)
in a single triple-homed container and never opens `bot-bottle.db` — it reaches
the supervise queue over the control-plane RPC (#469), so it holds no signing
key, only the pre-minted `gateway` token the orchestrator hands it via
`connect_to_orchestrator`.

This module also holds the pieces the infra service + launch/provision glue
share: the network names, the gateway image, and the network-creation helper.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...gateway import (
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_CA_CERT,
    MITMPROXY_HOME,
    Gateway,
    GatewayError,
    GatewayTransport,
)
from ...paths import (
    ORCHESTRATOR_AUTH_JWT_ENV,
    host_gateway_ca_dir,
)
from ... import resources
from .. import util as backend_util
from . import util as container_mod
from ...image_registry import RegistryError
from ...infra_image import pinned_infra_artifact, pull_pinned_image
from ...published_artifacts import FORMAT_OCI

# The shared host-only network the gateway container and every agent bottle sit
# on. The agent's address here is the attribution key. Distinct from the docker
# names so both backends can coexist on one host.
GATEWAY_NETWORK = "bot-bottle-mac-gateway"
# The NAT network that gives the gateway (and only it) a route out.
GATEWAY_EGRESS_NETWORK = "bot-bottle-mac-egress"
# The control network the gateway reaches the orchestrator over (host-only).
# Only the orchestrator + gateway join it; agents never do, so agents have no
# route to the control plane (PRD 0070 "Separating the planes").
CONTROL_NETWORK = "bot-bottle-mac-control"

# The gateway (data plane) container. The name predates the split — it was the
# combined "infra" container — and is kept so callers importing it still resolve
# the gateway (probe / reprovision attribute against it).
GATEWAY_NAME = "bot-bottle-mac-infra"
GATEWAY_LABEL = "bot-bottle-mac-infra=1"
# The gateway subset the consolidated model runs (no per-bottle git:// daemon).
GATEWAY_DAEMONS = "egress,git-http,supervise"

GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")


def ensure_networks(
    network: str = GATEWAY_NETWORK,
    egress_network: str = GATEWAY_EGRESS_NETWORK,
    control_network: str = CONTROL_NETWORK,
) -> None:
    """Create the shared host-only agent network, the NAT egress network, and
    the host-only control network. Idempotent — `create_network` tolerates
    'already exists'."""
    container_mod.create_network(egress_network)
    container_mod.create_network(network, internal=True)
    container_mod.create_network(control_network, internal=True)


class MacosGateway(Gateway):
    """The consolidated gateway as a single Apple container, triple-homed on the
    NAT egress, host-only agent, and control networks.

    `ensure_built` builds `Dockerfile.gateway`; the networks are ensured by the
    composer. `connect_to_orchestrator` runs the container carrying the mitmproxy
    CA + the pre-minted `gateway` token."""

    def __init__(
        self,
        image_ref: str = GATEWAY_IMAGE,
        *,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        control_network: str = CONTROL_NETWORK,
        repo_root: Path | None = None,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self.network = network
        self.egress_network = egress_network
        self.control_network = control_network
        # Build context: the repo root in a checkout, a staged copy from the
        # installed wheel otherwise (bot_bottle.resources).
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        # Set by `connect_to_orchestrator`: the URL the daemons resolve policy
        # against + the pre-minted `gateway` token they present. The gateway
        # never mints, so it never holds the signing key (#469).
        self._orchestrator_url = ""
        self._gateway_token = ""

    def ensure_built(self) -> None:
        """Build the data-plane image from `Dockerfile.gateway`. Pulls the release-pinned image instead when it was built
        from exactly this install's code (PRD 0081 / 0085)."""
        pin = pinned_infra_artifact("bot-bottle-gateway", FORMAT_OCI)
        if pin is not None:
            try:
                self.image_ref = pull_pinned_image(
                    pin, local_ref=self.image_ref, image_exists=container_mod.image_exists,
                    tag_image=container_mod.tag_image,
                    pull_argv=("container", "image", "pull"))
            except RegistryError as e:
                raise GatewayError(str(e)) from e
            return
        container_mod.build_image(
            self.image_ref, str(self._repo_root), dockerfile="Dockerfile.gateway")

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        """Bind the gateway to this orchestrator and bring it up, dual-homed on
        the agent + control networks, resolving policy against `orchestrator_url`
        (the orchestrator's control-network address — Apple has no container
        DNS) and presenting `gateway_token`.

        A running gateway on the current image, already bound to this
        orchestrator, is adopted untouched (returns False). Recreating it on
        every launch, as this used to, wiped every running bottle's git-gate
        state (#512). Otherwise it is (re)started (returns True)."""
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        # Fail closed on a missing policy source or token: the resolver-only data
        # plane (PRD 0070) would only crash-loop its daemons without an
        # orchestrator URL, and it cannot mint the token it presents (#469).
        if not self._orchestrator_url:
            raise GatewayError(
                "gateway requires an orchestrator URL to run "
                "(resolver-only data plane; no single-tenant fallback)"
            )
        if not self._gateway_token:
            raise GatewayError(
                "gateway requires a pre-minted `gateway` token to run "
                "(the orchestrator mints it; the gateway never holds the key)"
            )
        if self.is_running() and self._is_current(orchestrator_url, gateway_token):
            return False
        container_mod.force_remove_container(self.name)
        argv = [
            "container", "run", "--detach",
            "--name", self.name,
            "--label", "bot-bottle.backend=macos-container",
            "--label", GATEWAY_LABEL,
            # NAT egress FIRST (default route out); the host-only agent network
            # is where agents reach the gateway; the control network reaches the
            # orchestrator.
            "--network", self.egress_network,
            "--network", self.network,
            "--network", self.control_network,
            "--dns", container_mod.dns_server(),
            # The mitmproxy CA on a host bind-mount (survives recreation +
            # volume pruning — issue #450). No DB mount: the data plane never
            # opens bot-bottle.db (#469).
            "--mount",
            container_mod.bind_mount_spec(str(host_gateway_ca_dir()), MITMPROXY_HOME),
            "--env", f"BOT_BOTTLE_GATEWAY_DAEMONS={GATEWAY_DAEMONS}",
            "--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={self._orchestrator_url}",
            # The pre-minted `gateway` JWT (never the signing key). Bare
            # `--env NAME` inherits the value from run_env below.
            "--env", ORCHESTRATOR_AUTH_JWT_ENV,
            self.image_ref,
        ]
        run_env = {**os.environ, ORCHESTRATOR_AUTH_JWT_ENV: self._gateway_token}
        result = container_mod.run_container_argv(argv, env=run_env)
        if result.returncode != 0:
            raise GatewayError(
                f"gateway container failed to start: "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )
        return True

    def _is_current(self, orchestrator_url: str, gateway_token: str) -> bool:
        """True iff the running gateway was created from the current image,
        resolves policy against `orchestrator_url`, and presents
        `gateway_token`. Anything unreadable counts as not current: recreating
        costs a reattach, while keeping a stale gateway could keep it on an
        old image or a rotated-out token."""
        running = container_mod.container_image_digest(self.name)
        wanted = container_mod.image_digest(self.image_ref)
        if not running or running != wanted:
            return False
        env = container_mod.container_env(self.name)
        return (env.get("BOT_BOTTLE_ORCHESTRATOR_URL") == orchestrator_url
                and env.get(ORCHESTRATOR_AUTH_JWT_ENV) == gateway_token)

    def is_running(self) -> bool:
        return container_mod.container_is_running(self.name)

    def persisted_ca_dir(self) -> Path | None:
        return host_gateway_ca_dir()

    def stop(self) -> None:
        """Remove the gateway container (idempotent)."""
        container_mod.force_remove_container(self.name)

    def address(self) -> str:
        """The gateway's agent-network address — the proxy / git-http / supervise
        target agents dial (also the source IP the gateway attributes by)."""
        ip = container_mod.try_container_ipv4_on_network(self.name, self.network)
        if not ip:
            raise GatewayError(
                f"gateway {self.name} has no address on {self.network}"
            )
        return ip

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception. Read from the gateway container; polls because mitmproxy
        writes it a beat after start."""
        def _fetch() -> str | None:
            result = container_mod.run_container_argv(
                ["container", "exec", self.name, "cat", GATEWAY_CA_CERT])
            return result.stdout if result.returncode == 0 and result.stdout.strip() else None
        try:
            return backend_util.poll_ca_cert(_fetch, timeout=timeout)
        except TimeoutError as exc:
            raise GatewayError(
                f"gateway CA not available in {self.name} after {timeout:g}s"
            ) from exc

    def provisioning_transport(self) -> GatewayTransport:
        """The exec/cp transport git-gate provisioning stages per-bottle repos +
        deploy keys through (over the `container` CLI)."""
        # Local import: gateway_transport imports GATEWAY_NAME from this module,
        # so importing MacosGatewayTransport at module scope would cycle.
        from .gateway_transport import MacosGatewayTransport
        return MacosGatewayTransport(self.name)


__all__ = [
    "GATEWAY_NETWORK",
    "GATEWAY_EGRESS_NETWORK",
    "CONTROL_NETWORK",
    "GATEWAY_NAME",
    "GATEWAY_LABEL",
    "GATEWAY_DAEMONS",
    "GATEWAY_IMAGE",
    "GatewayError",
    "DEFAULT_CA_TIMEOUT_SECONDS",
    "ensure_networks",
    "MacosGateway",
]
