"""`MacosGateway`: the gateway as one Apple container on three networks. It
never opens `bot-bottle.db` and holds only the `gateway` token. Also holds the
network names and creation helper the infra service and launch share.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...environment import macos_agent_subnet_v6, resource_prefix
from ...control.gateway import (
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_CA_CERT,
    MITMPROXY_HOME,
    Gateway,
    GatewayError,
    GatewayTransport,
)
from ...contract.hostctl import DEFAULT_GATEWAY_IMAGE, Settings
from ...contract.identity import ORCHESTRATOR_AUTH_JWT_ENV
from ...lib.log import die
from ...paths import host_gateway_ca_dir
from ... import resources
from .. import util as backend_util
from . import host_firewall
from . import util as container_mod
from ...image_registry import RegistryError
from ...infra_image import (
    BUILD_WITH_CONTAINER, local_build_tag, note_local_build, pinned_infra_artifact,
    pull_pinned_image,
)
from ...published_artifacts import FORMAT_OCI

# The shared host-only network for the gateway and every agent; an agent's
# address here is its attribution key. Named apart from docker's so both
# backends can coexist.
GATEWAY_NETWORK = resource_prefix() + "mac-gateway"
# The agent network's IPv6 subnet, pinned so the host firewall can name it.
# The IPv4 one is whatever the installed firewall blocks
# (`host_firewall.agent_subnet`), chosen at setup to avoid this Mac's LAN,
# VPNs and other interfaces.
AGENT_SUBNET_V6 = macos_agent_subnet_v6()
# NAT network giving only the gateway a route out.
GATEWAY_EGRESS_NETWORK = resource_prefix() + "mac-egress"
# Host-only network the gateway reaches the orchestrator over. Agents never
# join it.
CONTROL_NETWORK = resource_prefix() + "mac-control"

# The gateway container. The name predates the split from the combined infra
# container.
GATEWAY_NAME = resource_prefix() + "mac-infra"
GATEWAY_LABEL = f"{GATEWAY_NAME}=1"
# The daemons to run (no git:// daemon).
GATEWAY_DAEMONS = "egress,git-http,supervise"

GATEWAY_IMAGE = DEFAULT_GATEWAY_IMAGE


def ensure_networks(
    network: str = GATEWAY_NETWORK,
    egress_network: str = GATEWAY_EGRESS_NETWORK,
    control_network: str = CONTROL_NETWORK,
) -> None:
    """Create the agent, egress and control networks. Idempotent. Dies when
    the agent network exists with other subnets than the host firewall
    blocks, or when something else on this Mac uses its subnet."""
    subnet = host_firewall.agent_subnet()
    clashes = host_firewall.conflicts(subnet)
    if clashes:
        die(f"the agent network's {subnet} is also used on this Mac "
            f"({'; '.join(clashes)}), so agents could reach it or cut it off. "
            "Run `bot-bottle install --backend=macos-container` to move "
            "agents to a free range.")
    container_mod.create_network(egress_network)
    container_mod.create_network(
        network, internal=True, subnet=subnet, subnet_v6=AGENT_SUBNET_V6)
    container_mod.create_network(control_network, internal=True)
    found = container_mod.network_subnets(network)
    if found != (subnet, AGENT_SUBNET_V6):
        die(
            f"network {network} has subnets {found}, not the "
            f"{subnet} and {AGENT_SUBNET_V6} the host firewall blocks. "
            f"Stop running bottles, then `container network rm {network}`; "
            "the next launch recreates it."
        )


class MacosGateway(Gateway):
    """The gateway as one Apple container on the NAT egress, host-only agent
    and control networks. `ensure_built` builds `Dockerfile.gateway`;
    `connect_to_orchestrator` runs it with the CA and the `gateway` token."""

    def __init__(
        self,
        image_ref: str | None = None,
        *,
        settings: Settings,
        name: str = GATEWAY_NAME,
        network: str = GATEWAY_NETWORK,
        egress_network: str = GATEWAY_EGRESS_NETWORK,
        control_network: str = CONTROL_NETWORK,
        repo_root: Path | None = None,
    ) -> None:
        self.image_ref = image_ref or settings.infra.gateway_image
        self._settings = settings
        self.name = name
        self.network = network
        self.egress_network = egress_network
        self.control_network = control_network
        # Build context: the checkout root, or a copy staged from the wheel.
        self._repo_root = repo_root if repo_root is not None else resources.build_root()
        # Set by `connect_to_orchestrator`: the policy URL and the `gateway`
        # token.
        self._orchestrator_url = ""
        self._gateway_token = ""

    def ensure_built(self) -> None:
        """Build the gateway image from `Dockerfile.gateway`, or pull the
        release-pinned image when it was built from this install's code.
        Builds only when asked (`up --build`); otherwise stops."""
        try:
            pin = pinned_infra_artifact(
                "bot-bottle-gateway", FORMAT_OCI,
                local_build=self._settings.infra.infra_build_local,
                build_note=BUILD_WITH_CONTAINER)
        except RegistryError as e:
            raise GatewayError(str(e)) from e
        if pin is not None:
            try:
                self.image_ref = pull_pinned_image(
                    pin, local_ref=self.image_ref, image_exists=container_mod.image_exists,
                    tag_image=container_mod.tag_image,
                    pull_argv=("container", "image", "pull"))
            except RegistryError as e:
                raise GatewayError(str(e)) from e
            return
        # Reuse a local build of exactly this code; even a cached Apple build
        # costs ~10 s.
        built = local_build_tag("bot-bottle-gateway", self.image_ref)
        if not self._settings.operation.no_cache and container_mod.image_exists(built):
            self.image_ref = built
            return
        note_local_build()
        container_mod.build_image(
            self.image_ref, str(self._repo_root), dockerfile="Dockerfile.gateway",
            no_cache=self._settings.operation.no_cache,
            dns_override=self._settings.infra.macos_container_dns)
        container_mod.tag_image(self.image_ref, built)
        self.image_ref = built

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        """Bind the gateway to this orchestrator (by control-network IP; Apple
        has no container DNS) and bring it up. A running gateway on the current
        image already bound to it is adopted (False), since recreating it wipes
        every bottle's git-gate state. Otherwise (re)started (True)."""
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        # Without a policy URL and token the daemons would only crash-loop.
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
            # NAT egress first (default route), then the agent and control
            # networks.
            "--network", self.egress_network,
            "--network", self.network,
            "--network", self.control_network,
            "--dns", container_mod.dns_server(self._settings.infra.macos_container_dns),
            # The CA on a host mount (survives recreation and pruning). No DB
            # mount.
            "--mount",
            container_mod.bind_mount_spec(str(host_gateway_ca_dir()), MITMPROXY_HOME),
            "--env", f"BOT_BOTTLE_GATEWAY_DAEMONS={GATEWAY_DAEMONS}",
            "--env", f"BOT_BOTTLE_ORCHESTRATOR_URL={self._orchestrator_url}",
            # The `gateway` token as a bare `--env NAME`, from run_env.
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
        """True iff the running gateway uses the current image,
        `orchestrator_url` and `gateway_token`. Anything unreadable counts as
        stale."""
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
        """Remove the gateway container. Idempotent."""
        container_mod.force_remove_container(self.name)

    def address(self) -> str:
        """The gateway's agent-network address: the agents' proxy target."""
        ip = container_mod.try_container_ipv4_on_network(self.name, self.network)
        if not ip:
            raise GatewayError(
                f"gateway {self.name} has no address on {self.network}"
            )
        return ip

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's CA PEM, read from the container and polled for."""
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
        """The exec/cp transport for git-gate provisioning, via the `container`
        CLI."""
        # Local import to avoid a cycle with gateway_transport.
        from .gateway_transport import MacosGatewayTransport
        return MacosGatewayTransport(self.name)


__all__ = [
    "AGENT_SUBNET_V6",
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
