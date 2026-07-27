"""The consolidated per-host gateway (PRD 0070).

The core consolidation win: **one** persistent gateway per host, shared by
every bottle, instead of a gateway per bottle. It's safe to share
because the attribution invariant (source IP + identity token, see
`registry`) lets the gateway attribute each request to the right bottle —
so per-bottle policy lives in one long-lived process keyed on who's calling.

`Gateway` is the backend-neutral service contract: bind the single instance to
its orchestrator and bring it up (`connect_to_orchestrator`), report its
agent-facing address + CA, vend the provisioning transport, tear it down. The
docker implementation (`DockerGateway`) lives in `backend/docker/gateway.py`;
the macOS + firecracker gateways slot in beside it.

The defining behaviour is **idempotent singleton**: `connect_to_orchestrator`
starts the instance if absent and is a no-op if it's already up on the same
binding, so N bottle launches never spawn N gateways.
"""

from __future__ import annotations

import abc
import os
from pathlib import Path
from typing import Protocol

from ..paths import host_gateway_ca_dir

# The gateway's mitmproxy writes its CA a beat after the container starts, so
# reads poll for it rather than assuming it's there on a fresh launch.
CA_POLL_SECONDS = 0.5
DEFAULT_CA_TIMEOUT_SECONDS = 30.0

GATEWAY_NAME = "bot-bottle-orch-gateway"
GATEWAY_LABEL = "bot-bottle-orch-gateway=1"
# The single user-defined network the gateway and every agent bottle share.
# Agents attach here with a pinned IP and reach the gateway's egress /
# git-http / supervise ports by its address — no host port publishing, and
# the source IP the gateway attributes by is the address on this network.
GATEWAY_NETWORK = "bot-bottle-gateway"

# mitmproxy's CA dir in the bundle. The host's gateway-CA dir (see
# `host_gateway_ca_dir`) is bind-mounted here so the gateway's self-generated
# CA stays STABLE across container recreation — every agent installs this one
# CA to trust the shared gateway's TLS interception, so it must not rotate when
# the gateway restarts. A host bind-mount rather than a named volume: a named
# volume is silently wiped by `docker volume prune`, minting a fresh CA that
# breaks every running bottle (issue #450).
MITMPROXY_HOME = "/home/mitmproxy/.mitmproxy"
GATEWAY_CA_CERT = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"

# The CA material mitmproxy writes into its confdir. mitmproxy reuses these on
# startup when present and generates them only on first run, so persisting them
# is what makes the CA stable; deleting them (see `rotate_gateway_ca`) forces a
# fresh CA on the next start. `mitmproxy-ca.pem` (cert + private key) is the
# signing identity; the rest are derived encodings agents/clients consume.
GATEWAY_CA_GLOB = "mitmproxy-ca*"

# The gateway data-plane image + its Dockerfile. Kept as a local constant
# rather than imported from the backend layer, which would drag
# the whole backend layer into the lean orchestrator (see #359); unify when
# that lands. Env override matches the backend's BOT_BOTTLE_GATEWAY_IMAGE.
GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")
GATEWAY_DOCKERFILE = "Dockerfile.gateway"


def rotate_gateway_ca(ca_dir: Path | None = None) -> list[Path]:
    """Delete the persisted mitmproxy CA so the next gateway start mints a
    fresh one — the explicit, deliberate CA-rollover path (issue #450).

    Persistence keeps the CA stable across restarts precisely because mitmproxy
    reuses the on-disk CA; rotation is therefore just removing that material.
    Returns the files removed (empty when there was no CA yet); idempotent.

    This only clears the on-disk CA. It does NOT stop the running gateway (whose
    mitmproxy still holds the old CA in memory) or re-provision agents — the
    caller recreates the gateway to mint the new CA and re-attaches bottles.
    `rotate-ca` on the orchestrator CLI wires those steps together."""
    ca_dir = ca_dir if ca_dir is not None else host_gateway_ca_dir()
    removed: list[Path] = []
    for path in sorted(ca_dir.glob(GATEWAY_CA_GLOB)):
        path.unlink()
        removed.append(path)
    return removed


class GatewayError(Exception):
    """The shared gateway failed to build/start/stop (non-zero `docker` exit)."""


class GatewayProvisionError(RuntimeError):
    """A git-gate provisioning step against the running gateway failed."""


class GatewayTransport(Protocol):
    """How the launcher stages files + runs commands in the running gateway.
    Backend-neutral so the same git-gate provisioning logic serves the docker
    gateway container (exec/cp over the docker socket), the Apple gateway
    container, and the firecracker gateway VM (over SSH)."""

    def exec(self, argv: list[str]) -> None:
        """Run `argv` in the gateway, raising `GatewayProvisionError` on failure."""

    def cp_into(self, src: str, dest: str) -> None:
        """Copy host file `src` to `dest` in the gateway, raising on failure."""


class Gateway(abc.ABC):
    """Provision + interact with the per-host gateway (data plane).

    One concrete impl per backend (`backend/*/gateway.py`); the host composes it
    with the Orchestrator service. The gateway **never holds the signing key** —
    it receives a pre-minted `gateway` token from the orchestrator and only
    presents it. Backend-neutral."""

    name: str

    def ensure_available(self) -> None:
        """Acquire the exact image/rootfs selected for this application."""
        self.ensure_built()

    def ensure_built(self) -> None:
        """Local-build implementation hook retained for backend compatibility."""
        return

    @abc.abstractmethod
    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> None:
        """Bind the gateway to this orchestrator and bring it up: store the URL +
        the pre-minted `gateway` token as instance state, then (re)start the
        gateway unit carrying the mitmproxy CA + that token, resolving policy
        against `orchestrator_url`. Idempotent — a healthy, current gateway on
        the same binding is left alone; a changed binding reconciles it."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the gateway instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the gateway. Idempotent — absent is success."""

    @abc.abstractmethod
    def address(self) -> str:
        """The agent-facing address agents dial for egress / git-http / supervise
        (today's `gateway_ip`)."""

    @abc.abstractmethod
    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The mitmproxy CA (PEM) agents install to trust the gateway's TLS
        interception. Polls — mitmproxy writes it a beat after start."""

    @abc.abstractmethod
    def provisioning_transport(self) -> "GatewayTransport":
        """The cp/exec transport git-gate provisioning uses to place per-bottle
        repos + deploy keys into the running gateway."""


__all__ = [
    "Gateway", "GatewayError", "GatewayProvisionError", "GatewayTransport",
    "rotate_gateway_ca",
    "GATEWAY_NAME", "GATEWAY_LABEL", "GATEWAY_IMAGE", "GATEWAY_NETWORK",
    "GATEWAY_CA_CERT", "GATEWAY_CA_GLOB",
]
